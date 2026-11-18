import logging
from decimal import Decimal
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from marketplace.models import MarketplaceOrder
from marketplace.utils import create_logistics_waybill, mark_paid_for_vendor_delivery
from payment.models import MarketplaceTransaction

from .models import VendorWallet

logger = logging.getLogger(__name__)

# Every status an order can legitimately be in AFTER payment was confirmed.
# Webhook, redirect verification and the verify endpoint can race in any order,
# so each of them must treat all of these as "already paid".
PAID_ORDER_STATES = (
    MarketplaceOrder.OrderStatus.PROCESSING,
    MarketplaceOrder.OrderStatus.PAID_UNASSIGNED,
    MarketplaceOrder.OrderStatus.IN_TRANSIT,
    MarketplaceOrder.OrderStatus.COMPLETED,
)


def get_paystack_bank_list():
    """
    Fetches the live list of Nigerian banks from Paystack and caches it for 24 hours.
    Returns {bank name: bank code}; an empty dict if Paystack is unreachable.
    """
    cache_key = 'paystack_nigerian_banks'
    cached_banks = cache.get(cache_key)
    if cached_banks:
        return cached_banks

    try:
        response = httpx.get(
            "https://api.paystack.co/bank",
            params={"country": "nigeria", "perPage": 200},
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            timeout=10.0,
        )
        res_data = response.json()
        if res_data.get('status') and res_data.get('data'):
            bank_map = {bank['name']: bank['code'] for bank in res_data['data']}
            cache.set(cache_key, bank_map, 86400)
            return bank_map
    except (httpx.RequestError, ValueError):
        logger.exception("Failed to fetch the live bank list from Paystack")

    return {}


def _paystack_confirms(order, reference) -> bool:
    """
    True only if Paystack itself reports a successful charge for THIS order:
    the reference must be the one stored on the order, the currency NGN and the
    amount at least order.total_price.

    order.total_price already includes the wallet discount (apply_wallet_to_order
    reduces it before Paystack is charged), so it is exactly what Paystack must
    have collected. Subtracting wallet_applied again would double-count the
    discount and accept underpayments.
    """
    if not reference or reference != order.payment_reference:
        return False
    try:
        res = httpx.get(
            f"https://api.paystack.co/transaction/verify/{quote(reference, safe='')}",
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            timeout=12.0,
        )
        body = res.json()
    except (httpx.RequestError, ValueError):
        logger.exception("Paystack verify failed for order %s", order.id)
        return False

    data = body.get("data") or {}
    paid_naira = Decimal(str(data.get("amount", 0))) / Decimal("100")
    expected = Decimal(str(order.total_price))

    if (
        body.get("status") is True
        and data.get("status") == "success"
        and data.get("reference") == order.payment_reference
        and data.get("currency", "NGN") == "NGN"
        and paid_naira >= expected
    ):
        return True

    logger.warning(
        "Paystack verification rejected for order %s: api_status=%s, tx_status=%s, ref=%s vs %s, paid=%s vs expected=%s",
        order.id, body.get("status"), data.get("status"),
        data.get("reference"), order.payment_reference, paid_naira, expected,
    )
    return False


def _queue_late_payment_refund(order, reference):
    """
    The customer paid for an order that had already been cancelled/released. Money was taken for
    something we will not fulfil, so a refund request is opened for finance automatically
    (idempotent: one open request per order) and the admin desk is alerted. Nothing is refunded
    by code; finance approves it from the existing refund queue.
    """
    from audit.models import OrderDisputeRefund

    marker = "[AUTO] Late payment received for cancelled order"
    try:
        with transaction.atomic():
            if OrderDisputeRefund.objects.filter(order_id=str(order.id), reason__startswith=marker).exists():
                return
            customer_user = order.customer.user if (order.customer and order.customer.user_id) else None
            refund = OrderDisputeRefund.objects.create(
                order_id=str(order.id),
                customer_name=((customer_user.get_full_name() or customer_user.email) if customer_user else "Customer")[:150],
                reason=f"{marker} (payment reference {reference}). Refund the customer in full.",
                amount=order.total_price,
                status=OrderDisputeRefund.StatusChoice.PENDING,
                raised_by="System (late payment)",
            )

            def _alert_admins():
                try:
                    from asgiref.sync import async_to_sync
                    from channels.layers import get_channel_layer
                    layer = get_channel_layer()
                    if layer:
                        async_to_sync(layer.group_send)("admin_notifications", {
                            "type": "admin_notification_message",
                            "message": {
                                "title": "Late payment: refund required",
                                "text": f"Order #{order.id} was paid after it was cancelled. A refund request of \u20a6{refund.amount:,.2f} was opened.",
                                "dispute_id": refund.id,
                            },
                        })
                except Exception:
                    logger.exception("Admin alert failed for late-payment refund %s", order.id)

            transaction.on_commit(_alert_admins)
    except Exception:
        logger.exception("Could not queue the late-payment refund for order %s", order.id)
        
def verify_order_payment(order_id, transaction_id=None):
    """
    Idempotent, race-safe payment confirmation. Returns True if the order is (now)
    paid. Called by the webhook, the redirect callback and the verify endpoint;
    the row lock guarantees the PENDING -> paid transition and the waybill happen
    exactly once.
    """
    order = MarketplaceOrder.objects.get(id=order_id)

    if order.status in PAID_ORDER_STATES:
        return True

    reference = transaction_id or order.payment_reference

    if order.status == MarketplaceOrder.OrderStatus.CANCELLED:
        # The customer paid after we released the order: money was taken for an
        # order we will not fulfil. Do not revive it silently; alert for a refund.
        if _paystack_confirms(order, reference):
            logger.critical(
                "PAID CANCELLED ORDER: order %s, reference %s. Refund required.",
                order.id, reference,
            )
        return False

    # Network call happens outside any DB transaction / lock.
    if not _paystack_confirms(order, reference):
        # The webhook may have settled it while we were waiting on the HTTP call.
        order.refresh_from_db(fields=['status'])
        return order.status in PAID_ORDER_STATES

    with transaction.atomic():
        order = (
            MarketplaceOrder.objects.select_for_update(of=('self',))
            .select_related('vendor', 'vendor__user', 'customer', 'customer__user')
            .get(id=order_id)
        )
        if order.status != MarketplaceOrder.OrderStatus.PENDING:
            return order.status in PAID_ORDER_STATES

        order.status = MarketplaceOrder.OrderStatus.PROCESSING
        order.save(update_fields=['status'])

        try:
            with transaction.atomic():  # savepoint: a failure must not poison this transaction
                if order.fulfillment_mode == MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY:
                    # The vendor delivers: no waybill, no rider pool.
                    mark_paid_for_vendor_delivery(order)
                else:
                    create_logistics_waybill(order)
        except Exception:
            # The payment stays recorded (PROCESSING). For rider orders the `reconcile_waybills`
            # command (or the merchant's acknowledgement) creates the missing waybill; vendor-
            # delivered orders can be acknowledged straight from PROCESSING.
            logger.exception("Post-payment dispatch step failed for paid order %s; needs retry", order.id)

    return True


def process_order_revenue_split(marketplace_order, force=False, include_delivery_fee=False):
    """
    Credits 100% of the basket value to the vendor wallet when the rider picks the
    order up. Race-safe: the order row is locked, the ledger row is unique per
    order, and the wallet is credited with an atomic F() update (no lost updates
    between concurrent orders of the same vendor).
    """
    with transaction.atomic():
        order = (
            MarketplaceOrder.objects.select_for_update(of=('self',))
            .select_related('vendor')
            .get(pk=marketplace_order.pk)
        )

        if not force and MarketplaceTransaction.objects.filter(order_id=str(order.id)).exists():
            logger.info("Revenue split skipped: order %s already settled", order.id)
            return False

        vendor_profile = order.vendor
        if not vendor_profile:
            raise ValueError(f"Order #{order.id} has no assigned vendor.")

        total_paid = Decimal(str(order.total_price))
        delivery_fee = Decimal(str(order.delivery_fee))
        basket_value = total_paid - delivery_fee
        if basket_value <= 0:
            raise ValueError(f"Abnormal settlement math for Order #{order.id}: Basket is {basket_value}")

        platform_fee_collected = Decimal('0.00')
        # 100% of the basket goes to the vendor; for vendor-delivered orders the vendor also
        # keeps the delivery charge it set (it is not a platform/rider fee).
        vendor_share = total_paid if include_delivery_fee else basket_value

        wallet, _created = VendorWallet.objects.select_for_update().get_or_create(profile=vendor_profile)
        VendorWallet.objects.filter(pk=wallet.pk).update(
            available_balance=F('available_balance') + vendor_share,
            updated_at=timezone.now(),
        )

        MarketplaceTransaction.objects.update_or_create(
            order_id=str(order.id),
            defaults=dict(
                vendor_profile=vendor_profile,
                total_basket_value=basket_value,
                platform_fee_collected=platform_fee_collected,
                vendor_payout_share=vendor_share,
                paystack_reference=order.payment_reference or f"REF{order.id}",
            ),
        )

        logger.info("Order %s settled: vendor wallet credited with %s", order.id, vendor_share)

    return True
