import logging
import math
import uuid
from decimal import Decimal, ROUND_HALF_UP

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import F

from accounts.models import CoverageArea
from customers.models import CustomerWallet, CustomerWalletTransaction
from logistics.models import LogisticsPricingConfig
from order.models import Order

from .models import MarketplaceOrder

logger = logging.getLogger(__name__)

FALLBACK_PHONE = "08000000000"


# =========================================================================
# LOGISTICS WAYBILL CREATION
# =========================================================================
def _broadcast_new_order(vendor_id: str, payload: dict):
    """
    Pushes the new-order alert to the vendor's dashboard group. Always invoked
    through transaction.on_commit so a vendor is never notified about an order
    whose transaction later rolls back. Never raises.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f"vendor_{str(vendor_id).strip()}",
            {"type": "send_order_notification", "event": "new_order", **payload},
        )
    except Exception:
        logger.exception("Vendor WebSocket broadcast failed for payload %s", payload.get("order_id"))


def create_logistics_waybill(marketplace_order):
    """
    Turns a paid MarketplaceOrder into a logistics waybill.

    * Idempotent: if the order already has a waybill it is returned untouched, so
      webhook/redirect/reconcile races can never create duplicates.
    * Must run inside the caller's transaction (or opens its own). Wrap the call in
      a savepoint (`with transaction.atomic():`) when a failure must not abort the
      surrounding transaction.
    """
    if marketplace_order.logistics_dispatch_id:
        return marketplace_order.logistics_dispatch

    vendor = marketplace_order.vendor
    customer = marketplace_order.customer

    # --- customer resolution (handles orders with no customer profile) ---
    if customer and getattr(customer, "user", None):
        customer_user = customer.user
        resolved_customer_name = f"{customer_user.first_name} {customer_user.last_name}".strip()
        customer_phone = getattr(customer_user, "phone_number", "") or FALLBACK_PHONE
    else:
        resolved_customer_name = "Marketplace Admin / Partner"
        customer_phone = FALLBACK_PHONE
    resolved_customer_name = resolved_customer_name or "Marketplace Client"

    business_type_str = str(getattr(vendor, "business_type", "e_commerce")).lower().strip()
    logistics_package_type = (
        Order.PackageType.FOOD if "restaurant" in business_type_str else Order.PackageType.OTHER
    )

    # --- pickup address: base_location is a CoverageArea object, use its name ---
    vendor_user = getattr(vendor, "user", None)
    sender_street = (getattr(vendor_user, "street_address", "") or "") if vendor_user else ""
    base_area = getattr(vendor_user, "base_location", None) if vendor_user else None
    sender_base = getattr(base_area, "name", "") if base_area else ""
    full_pickup_address = (
        ", ".join(part for part in (sender_street, sender_base) if part)
        or marketplace_order.base_location
        or "Vendor Location"
    )
    sender_phone_num = (getattr(vendor_user, "phone_number", None) if vendor_user else None) or FALLBACK_PHONE

    # --- item description (one query: product is the common case) ---
    item_summaries = []
    for item in marketplace_order.items.select_related("product", "product_item__product"):
        product = item.product or (item.product_item.product if item.product_item else None)
        item_summaries.append(f"{item.quantity}x {product.name if product else 'Item'}")
    dynamic_description = (
        ", ".join(item_summaries) or f"Marketplace Order #{marketplace_order.id} items."
    )

    with transaction.atomic():
        logistics_waybill = Order.objects.create(
            marketplace_order=marketplace_order,
            sender=vendor,
            receiver=customer,
            sender_name=vendor.company_name,
            sender_phone=sender_phone_num,
            pickup_address=full_pickup_address,
            receiver_name=resolved_customer_name,
            receiver_phone=customer_phone,
            delivery_address=marketplace_order.delivery_address,
            package_type=logistics_package_type,
            package_description=dynamic_description,
            delivery_fee=marketplace_order.delivery_fee,
            is_cash_on_delivery=False,
            cod_amount_to_collect=Decimal("0.00"),
            status=Order.OrderStatus.PENDING,
        )

        marketplace_order.logistics_dispatch = logistics_waybill
        marketplace_order.status = MarketplaceOrder.OrderStatus.PAID_UNASSIGNED
        marketplace_order.save(update_fields=["logistics_dispatch", "status"])

    # --- real-time alert, only after the data is durably committed ---
    base_loc_str = str(marketplace_order.base_location or "")
    payload = {
        "order_id": str(marketplace_order.id),
        "total_price": float(marketplace_order.subtotal or 0),
        "delivery_address": f"{marketplace_order.delivery_address} | {base_loc_str}".strip(" |"),
        "items_summary": item_summaries,
        "customer_name": resolved_customer_name,
        "customer_phone": customer_phone,
    }
    transaction.on_commit(lambda: _broadcast_new_order(vendor.id, payload))

    return logistics_waybill


# =========================================================================
# FULFILLMENT ROUTING (platform riders vs. vendor self-delivery)
# =========================================================================
class AreaRequiredError(ValueError):
    """The buyer's delivery area is needed to pick a delivery option (the quote asks for it)."""


# =========================================================================
# FULFILLMENT ROUTING (platform riders vs. vendor self-delivery)
# =========================================================================
def _resolve_buyer_area(base_location_input, customer_profile):
    """
    Finds the buyer's CoverageArea. Accepts an id, "Name, State" or a name that identifies
    exactly ONE area (names repeat across states, so an ambiguous name is never guessed).
    Falls back to the customer's saved base location.
    """
    raw = str(base_location_input or '').strip()
    user = getattr(customer_profile, 'user', None)
    saved_area = getattr(user, 'base_location', None) if user else None

    if raw:
        if raw.isdigit():
            area = CoverageArea.objects.filter(id=int(raw)).first()
            if area:
                return area
        else:
            if ',' in raw:
                name, state = [part.strip() for part in raw.split(',', 1)]
                area = CoverageArea.objects.filter(name__iexact=name, state__name__iexact=state).first()
                if area:
                    return area
            name_only = raw.split(',')[0].strip()
            matches = list(CoverageArea.objects.filter(name__iexact=name_only)[:2])
            if len(matches) == 1:
                return matches[0]
            # Same name in several states: the customer's own saved hub settles it.
            if len(matches) > 1 and saved_area and saved_area.name.lower() == name_only.lower():
                return saved_area

    # Nothing usable was sent (e.g. free text): use the hub saved on the customer's account.
    return saved_area


def decide_fulfillment(vendor, customer_profile, base_location_input):
    """
    Decides who delivers an order. Returns (fulfillment_mode, self_delivery_fee or None).
    Raises ValueError (shown to the customer) when no valid option exists.

      * restaurants                -> always platform riders
      * no preference row          -> platform riders (legacy behaviour)
      * 'self'                     -> vendor delivers, at the vendor's own fee
      * 'hybrid'                   -> same hub: platform riders; other hub: vendor delivers
      * 'platform'                 -> platform riders; outside the vendor's hub only when
                                      allow_nationwide is on
    """
    Mode = MarketplaceOrder.FulfillmentMode
    is_restaurant = str(getattr(vendor, 'business_type', '') or '').lower() == 'restaurant'
    preference = getattr(vendor, 'shipping_preference', None)  # reverse one-to-one: None if missing

    if is_restaurant or preference is None:
        return Mode.PLATFORM_DISPATCH, None

    kind = preference.fulfillment_type
    if kind == 'self':
        return Mode.VENDOR_SELF_DELIVERY, Decimal(preference.self_delivery_fee or 0)

    vendor_user = getattr(vendor, 'user', None)
    vendor_area = getattr(vendor_user, 'base_location', None) if vendor_user else None
    buyer_area = _resolve_buyer_area(base_location_input, customer_profile)

    if kind == 'hybrid':
        if vendor_area is None:
            return Mode.PLATFORM_DISPATCH, None
        if buyer_area is None:
            raise ValueError("Please select your delivery area so we can choose the right delivery option.")
        if vendor_area.id == buyer_area.id:
            return Mode.PLATFORM_DISPATCH, None
        return Mode.VENDOR_SELF_DELIVERY, Decimal(preference.self_delivery_fee or 0)

    # 'platform'
    if (not preference.allow_nationwide and vendor_area is not None
            and buyer_area is not None and vendor_area.id != buyer_area.id):
        raise ValueError(f"{vendor.company_name} only delivers within {vendor_area.name}.")
    return Mode.PLATFORM_DISPATCH, None


def mark_paid_for_vendor_delivery(order):
    """
    Paid order that the VENDOR will deliver: no logistics waybill and no rider pool. It moves to
    PAID_UNASSIGNED (paid, waiting for the vendor to acknowledge) and the vendor dashboard is
    alerted after the commit.
    """
    order.status = MarketplaceOrder.OrderStatus.PAID_UNASSIGNED
    order.save(update_fields=["status"])

    customer = order.customer
    customer_user = customer.user if (customer and getattr(customer, "user", None)) else None
    customer_name = (
        f"{customer_user.first_name} {customer_user.last_name}".strip() if customer_user else ""
    ) or "Marketplace Client"
    customer_phone = (getattr(customer_user, "phone_number", "") if customer_user else "") or FALLBACK_PHONE

    summaries = [
        f"{item.quantity}x {item.get_product_name}"
        for item in order.items.select_related("product", "product_item__product")
    ]
    payload = {
        "order_id": str(order.id),
        "total_price": float(order.total_price or 0),
        "delivery_address": f"{order.delivery_address} | {order.base_location or ''}".strip(" |"),
        "items_summary": summaries,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "delivery_mode": "vendor_self_delivery",
    }
    transaction.on_commit(lambda: _broadcast_new_order(order.vendor_id, payload))


# =========================================================================
# PAYSTACK INITIALISATION
# =========================================================================
def _amount_in_kobo(amount) -> int:
    """Exact Decimal -> kobo conversion (float math truncates, e.g. 0.29*100 = 28.999...)."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _paystack_init_payload(order, customer_email) -> dict:
    return {
        "email": customer_email,
        "amount": _amount_in_kobo(order.total_price),
        "currency": "NGN",
        "reference": f"{order.id}{uuid.uuid4().hex[:6].upper()}",
        "callback_url": f"{str(settings.FRONTEND_URL).rstrip('/')}/marketplace/order/verify?order_id={order.id}",
        "metadata": {"order_id": str(order.id)},  # informational only, never used to find orders
    }


def _paystack_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _parse_paystack_init(response, order) -> dict:
    try:
        res_data = response.json()
    except ValueError:
        res_data = {}
    if response.status_code in (200, 201) and res_data.get("status"):
        data = res_data["data"]
        return {
            "status": "success",
            "checkout_url": data["authorization_url"],
            "reference": data["reference"],
        }
    logger.error("Paystack init rejected for order %s: %s", order.id, res_data.get("message"))
    raise Exception("Paystack initialization failed.")


async def initialize_paystack_checkout(order, customer_email):
    """Async variant (kept for callers running on the event loop)."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://api.paystack.co/transaction/initialize",
                json=_paystack_init_payload(order, customer_email),
                headers=_paystack_headers(),
                timeout=15.0,
            )
        except httpx.RequestError:
            logger.exception("Paystack initialize request failed for order %s", order.id)
            raise Exception("Payment gateway unreachable.")
    return _parse_paystack_init(response, order)


def initialize_paystack_checkout_sync(order, customer_email):
    """Sync variant used inside the per-order lock in checkout."""
    try:
        response = httpx.post(
            "https://api.paystack.co/transaction/initialize",
            json=_paystack_init_payload(order, customer_email),
            headers=_paystack_headers(),
            timeout=15.0,
        )
    except httpx.RequestError:
        logger.exception("Paystack initialize request failed for order %s", order.id)
        raise Exception("Payment gateway unreachable.")
    return _parse_paystack_init(response, order)


def ensure_payment_link(order_id, customer_email) -> dict:
    """
    Returns the order's Paystack link, creating it exactly once. The order row is
    locked while we talk to Paystack so a double-click cannot produce two
    references (the second one would overwrite the first and orphan a payment).
    """
    with transaction.atomic():
        order = MarketplaceOrder.objects.select_for_update().get(id=order_id)
        if order.payment_reference and order.payment_checkout_url:
            return {"reference": order.payment_reference, "checkout_url": order.payment_checkout_url}
        result = initialize_paystack_checkout_sync(order, customer_email)
        order.payment_reference = result["reference"]
        order.payment_checkout_url = result["checkout_url"]
        order.save(update_fields=["payment_reference", "payment_checkout_url"])
        return {"reference": result["reference"], "checkout_url": result["checkout_url"]}


# =========================================================================
# HELPERS
# =========================================================================
def parse_clean_id(raw_id: str) -> str:
    """Strips option suffixes like '12-opt-45' back to the base product ID '12'."""
    raw_str = str(raw_id)
    if "-opt-" in raw_str:
        return raw_str.split("-opt-")[0]
    return raw_str


def _valid_coordinate(value, limit):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if -limit <= number <= limit else None

def base_delivery_fare() -> Decimal:
    """The platform's minimum delivery fee (what a nearby order costs)."""
    config = LogisticsPricingConfig.objects.first()
    return Decimal(str(config.base_fare)) if config and config.base_fare is not None else Decimal("1000.00")
    

MAX_AREA_RADIUS_KM = 50  # a hub's delivery zone; coordinates farther than this are not believed

def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlng = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def sanitize_customer_coordinates(lat, lng, buyer_area):
    """
    The delivery fee is distance based and the coordinates come from the customer's device, so a
    customer could claim to be right next to the vendor to cut the fee. The coordinates are only
    trusted when they fall inside the selected delivery area; otherwise (or when missing) the
    area's own centre is used, which prices the order for the area the customer really chose.
    Returns (lat, lng); either may be None when nothing usable exists.
    """
    la, lo = _valid_coordinate(lat, 90), _valid_coordinate(lng, 180)
    area_lat = getattr(buyer_area, 'latitude', None)
    area_lng = getattr(buyer_area, 'longitude', None)
    if buyer_area is None or area_lat is None or area_lng is None:
        return la, lo  # nothing to compare with (run geocode_coverage_areas to fill area centres)

    centre = (float(area_lat), float(area_lng))
    if la is None or lo is None:
        return centre
    if _haversine_km(la, lo, centre[0], centre[1]) > MAX_AREA_RADIUS_KM:
        return centre
    return la, lo

def _calculate_distance_delivery_fee(vendor_lat, vendor_lng, customer_lat, customer_lng) -> Decimal:
    """Computes the delivery fee from live LogisticsPricingConfig records."""
    base_fare = Decimal("1000.00")
    per_km_rate = Decimal("150.00")
    included_base_km = Decimal("1.0")

    config = LogisticsPricingConfig.objects.first()
    if config:
        if config.base_fare is not None:
            base_fare = Decimal(str(config.base_fare))
        if config.per_kilometer_rate is not None:
            per_km_rate = Decimal(str(config.per_kilometer_rate))
        if hasattr(config, "included_base_km") and config.included_base_km is not None:
            included_base_km = Decimal(str(config.included_base_km))

    v_lat = _valid_coordinate(vendor_lat, 90)
    v_lng = _valid_coordinate(vendor_lng, 180)
    c_lat = _valid_coordinate(customer_lat, 90)
    c_lng = _valid_coordinate(customer_lng, 180)
    if not v_lat or not v_lng or not c_lat or not c_lng:
        return base_fare

    try:
        lat1, lon1 = math.radians(v_lat), math.radians(v_lng)
        lat2, lon2 = math.radians(c_lat), math.radians(c_lng)
        dlon, dlat = lon2 - lon1, lat2 - lat1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        distance_km = Decimal(str(6371 * c))
        billable_distance = max(Decimal("0.0"), distance_km - included_base_km)
        total_fee = base_fare + (billable_distance * per_km_rate)
        return total_fee.quantize(Decimal("0.01"))
    except Exception:
        return base_fare


# =========================================================================
# CUSTOMER WALLET
# =========================================================================
def apply_wallet_to_order(order):
    """
    Applies the customer's wallet balance against the delivery fee and records the
    amount on the order (order.wallet_applied) so it can be refunded if the order
    is never paid. The debit is an atomic F() update under a row lock.

    Example: delivery fee 1,000 and wallet 500 -> wallet covers 500, delivery fee
    and total price drop by 500, and Paystack is charged the reduced total.
    """
    customer_user = order.customer.user if order.customer else None
    if not customer_user:
        return Decimal("0.00")

    # The delivery fee of a vendor-delivered order belongs to the vendor, not to the platform.
    if order.fulfillment_mode == MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY:
        return Decimal("0.00")

    with transaction.atomic():
        try:
            wallet = CustomerWallet.objects.select_for_update().get(user=customer_user, is_active=True)
        except CustomerWallet.DoesNotExist:
            return Decimal("0.00")

        if wallet.balance <= Decimal("0.00"):
            return Decimal("0.00")

        amount_to_cover = min(order.delivery_fee, wallet.balance)
        if amount_to_cover <= Decimal("0.00"):
            return Decimal("0.00")

        CustomerWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") - amount_to_cover)
        CustomerWalletTransaction.objects.create(
            wallet=wallet,
            amount=amount_to_cover,
            transaction_type=CustomerWalletTransaction.TransactionType.DEBIT,
            reference=f"WAL-DEBIT-{uuid.uuid4().hex[:10].upper()}",
            description=f"Wallet applied to offset delivery fee for order {order.id}",
        )

        order.delivery_fee -= amount_to_cover
        order.total_price -= amount_to_cover
        order.wallet_applied = amount_to_cover
        order.save(update_fields=["delivery_fee", "total_price", "wallet_applied"])

    return amount_to_cover


def _cancel_locked_order(order_id, allowed_statuses):
    """
    Cancels an order under a row lock: restores reserved stock, refunds any wallet credit
    that was applied to it, and cancels its (not yet picked up) waybill.

    Returns (order, was_paid) or (None, False) when the order does not exist or is not in
    one of `allowed_statuses`. Calling it twice is harmless: the second call sees CANCELLED
    and does nothing, so stock and wallet credit are restored exactly once.
    """
    from .models import ProductItem  # local import: avoids a circular import at module load

    with transaction.atomic():
        try:
            order = (
                MarketplaceOrder.objects.select_for_update(of=('self',))
                .select_related('customer__user')
                .get(id=order_id)
            )
        except MarketplaceOrder.DoesNotExist:
            return None, False
        if order.status not in allowed_statuses:
            return None, False

        was_paid = order.status != MarketplaceOrder.OrderStatus.PENDING

        for item in order.items.all():
            if item.product_item_id:
                ProductItem.objects.filter(pk=item.product_item_id).update(
                    stock_qty=F("stock_qty") + item.quantity
                )

        if order.wallet_applied and order.wallet_applied > 0 and order.customer:
            wallet = (
                CustomerWallet.objects.select_for_update()
                .filter(user=order.customer.user)
                .first()
            )
            if wallet:
                CustomerWallet.objects.filter(pk=wallet.pk).update(
                    balance=F("balance") + order.wallet_applied
                )
                CustomerWalletTransaction.objects.create(
                    wallet=wallet,
                    amount=order.wallet_applied,
                    transaction_type=CustomerWalletTransaction.TransactionType.CREDIT,
                    reference=f"WAL-REFUND-{uuid.uuid4().hex[:10].upper()}",
                    description=f"Refund of wallet credit for cancelled order {order.id}",
                )

        if order.logistics_dispatch_id:
            Order.objects.filter(
                pk=order.logistics_dispatch_id,
                status__in=[
                    Order.OrderStatus.PENDING,
                    Order.OrderStatus.ACKNOWLEDGED,
                    Order.OrderStatus.ASSIGNED,
                ],
            ).update(status=Order.OrderStatus.CANCELLED)

        order.status = MarketplaceOrder.OrderStatus.CANCELLED
        order.save(update_fields=["status"])
        return order, was_paid


def release_unpaid_order(order_id) -> bool:
    """
    Releases a still-PENDING (unpaid) order: cancels it, restores stock and refunds wallet
    credit. Returns True if it released the order.
    """
    order, _ = _cancel_locked_order(order_id, (MarketplaceOrder.OrderStatus.PENDING,))
    return order is not None


def cancel_order_before_pickup(order_id):
    """
    Staff cancellation of an order that has not been picked up yet (unpaid, paid, or being
    prepared). Returns (order, was_paid); (None, False) if it can no longer be cancelled.
    The caller is responsible for starting the refund workflow when was_paid is True.
    """
    return _cancel_locked_order(order_id, (
        MarketplaceOrder.OrderStatus.PENDING,
        MarketplaceOrder.OrderStatus.PAID_UNASSIGNED,
        MarketplaceOrder.OrderStatus.PROCESSING,
    ))
