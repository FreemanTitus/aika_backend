import logging
import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import F

from core.models import PlatformSetting
from customers.models import CustomerWallet, CustomerWalletTransaction, Referral
from marketing.models import UserNotification

logger = logging.getLogger(__name__)


def process_order_referral_reward(order):
    """
    Grants the inviter's referral bonus once the invitee's first qualifying order is
    paid. Idempotent and race-safe: the Referral row is locked, so concurrent
    verify/redirect/webhook calls can never grant the reward twice.
    """
    customer_profile = order.customer
    if not customer_profile or not customer_profile.user:
        return

    user_instance = customer_profile.user

    with transaction.atomic():
        referral_record = (
            Referral.objects.select_for_update()
            .select_related('inviter')
            .filter(invitee=user_instance, reward_granted=False)
            .first()
        )
        if referral_record is None:
            return

        platform_settings = PlatformSetting.get_settings()
        reward_threshold = platform_settings.referral_order_threshold
        bonus_amount = platform_settings.referral_bonus_amount

        if order.subtotal_value < reward_threshold:
            return

        referral_record.has_completed_qualifying_order = True
        referral_record.qualifying_order_id = str(order.id)
        referral_record.reward_granted = True
        referral_record.save()

        inviter_wallet, _ = CustomerWallet.objects.select_for_update().get_or_create(
            user=referral_record.inviter
        )
        CustomerWallet.objects.filter(pk=inviter_wallet.pk).update(
            balance=F('balance') + bonus_amount
        )

        CustomerWalletTransaction.objects.create(
            wallet=inviter_wallet,
            amount=bonus_amount,
            transaction_type=CustomerWalletTransaction.TransactionType.REFERRAL_BONUS,
            reference=f"REF-BONUS-{uuid.uuid4().hex[:10].upper()}",
            description=f"Referral reward unlocked from invitee {user_instance.email}",
        )

        UserNotification.objects.create(
            user=referral_record.inviter,
            title="Referral Reward Unlocked!",
            message=(
                "Your invitee just completed a qualifying order! You've been rewarded "
                f"<b>₦{bonus_amount:,.2f}</b> into your Aika wallet."
            ),
            notification_type=UserNotification.NotificationType.REFERRAL_REWARD,
        )


def complete_marketplace_order(marketplace_id, actor_user, *, allow_vendor=False, system=False):
    """
    Closes out a delivered order: marks the logistics waybill and the marketplace order
    COMPLETED, frees the rider, credits the rider (dynamically based on employment type) 
    and books the platform's cut.

    Shared by the customer "complete" endpoint and the order confirm-delivery endpoint
    so the money movement lives in exactly ONE place and runs exactly once: the waybill
    row is locked and its status re-checked inside the transaction, so a double tap or
    two concurrent requests cannot pay the rider twice.

    Vendor-delivered orders (fulfillment_mode == vendor_self_delivery) have no waybill and no
    rider: completing them credits the VENDOR wallet (basket + the vendor's delivery charge). Only
    the customer can confirm those (the vendor must never be able to pay itself); `system=True`
    is reserved for the auto-confirm job.

    Raises:
      LookupError       order not found OR not owned by actor_user
      ValueError        order is not in a state that can be completed
    Returns (marketplace_order, logistics_order or None, rider_new_balance, rider_user_id)
    """
    # Imported lazily: these modules import from order.models and would otherwise
    # create a circular import at start-up.
    from aika_platform.models import PlatformLedger
    from marketplace.models import MarketplaceOrder
    from order.models import Order
    from rider.models import RiderProfile, RiderTransactionLedger, RiderWallet

    with transaction.atomic():
        try:
            m_order = (
                MarketplaceOrder.objects.select_for_update(of=('self',))
                .select_related('vendor', 'vendor__user', 'customer', 'customer__user', 'logistics_dispatch')
                .get(id=marketplace_id)
            )
        except MarketplaceOrder.DoesNotExist:
            raise LookupError("Order not found.")

        is_self_delivery = m_order.fulfillment_mode == MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY

        if not system:
            is_customer = bool(m_order.customer and m_order.customer.user_id == actor_user.id)
            is_vendor = bool(
                allow_vendor and not is_self_delivery
                and m_order.vendor and m_order.vendor.user_id == actor_user.id
            )
            if not (is_customer or is_vendor):
                raise LookupError("Order not found.")  # same answer as a missing order: no IDOR oracle

        if is_self_delivery:
            from wallet.utils import process_order_revenue_split

            if m_order.status != MarketplaceOrder.OrderStatus.IN_TRANSIT:
                raise ValueError("Order is not ready to be marked as completed.")

            m_order.status = MarketplaceOrder.OrderStatus.COMPLETED
            m_order.save(update_fields=['status'])
            process_order_revenue_split(m_order, force=False, include_delivery_fee=True)
            return m_order, None, 0.0, None

        if not m_order.logistics_dispatch_id:
            raise ValueError("Linked logistics order not found.")

        logistics_order = (
            Order.objects.select_for_update(of=('self',))
            .select_related('assigned_rider')
            .get(pk=m_order.logistics_dispatch_id)
        )

        # Updated to check against OrderStatus.DELIVERED based on your lean workflow
        if logistics_order.status not in (Order.OrderStatus.DELIVERED, Order.OrderStatus.IN_TRANSIT):
            raise ValueError("Order is not ready to be marked as completed.")

        logistics_order.status = Order.OrderStatus.DELIVERED
        logistics_order.save(update_fields=['status', 'updated_at'])

        m_order.status = MarketplaceOrder.OrderStatus.COMPLETED
        m_order.save(update_fields=['status'])

        rider = logistics_order.assigned_rider
        rider_new_balance = 0.0
        rider_user_id = None

        if rider:
            rider_user_id = rider.user_id

            # Pull the locked splits directly from the order (calculated dynamically 
            # via DeliveryConfig upon rider assignment)
            rider_earning = logistics_order.rider_earning
            platform_cut = logistics_order.platform_fee

            wallet, _ = RiderWallet.objects.select_for_update().get_or_create(rider=rider)
            RiderWallet.objects.filter(pk=wallet.pk).update(
                balance=F('balance') + rider_earning,
                total_earned=F('total_earned') + rider_earning,
            )
            wallet.refresh_from_db(fields=['balance'])
            rider_new_balance = float(wallet.balance)

            RiderTransactionLedger.objects.create(
                rider=rider,
                amount=rider_earning,
                transaction_type=RiderTransactionLedger.TransactionType.EARNING,
                description=f"Delivery earning ({rider.get_employment_type_display()}) for Order #{m_order.id}",
            )

            PlatformLedger.objects.create(
                source_type=PlatformLedger.RevenueSource.LOGISTICS_CUT,
                amount=platform_cut,
                logistics_order=logistics_order,
                description=f"Platform ₦{platform_cut:,.2f} logistics cut from Order #{m_order.id}",
            )

            # Free the rider for the next job (checking status or availability flag as appropriate for your schema)
            RiderProfile.objects.filter(pk=rider.pk).update(
                is_active_for_delivery=True
            )

        return m_order, logistics_order, rider_new_balance, rider_user_id