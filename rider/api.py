import logging

import httpx
from django.conf import settings
from ninja import Router
from django.core.exceptions import FieldDoesNotExist
from ninja.errors import HttpError
from django.db import transaction
from asgiref.sync import sync_to_async
from .models import RiderProfile, RiderPaymentAccount, RiderWallet
from marketplace.models import MarketplaceOrder
from .schemas import RiderDashboardResponse, RiderStatusSchema, ActiveOrdersListSchema, RiderPreferencesSchema
from order.models import Order
from accounts.utils import JWTAuth
from marketing.models import UserNotification
from channels.layers import get_channel_layer
from wallet.utils import process_order_revenue_split
from django.db.models import Count, Sum, Q
from decimal import Decimal


logger = logging.getLogger(__name__)

router = Router(tags=["Riders"], auth=JWTAuth())

def _has_model_field(model, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False
    

def get_rider_wallet_stats(rider):
    """
    Computes financial statistics for a rider, pulling the actual spendable 
    balance directly from the persistent RiderWallet model.
    """
    # 1. Fetch or get the actual persistent wallet record
    wallet, _ = RiderWallet.objects.get_or_create(rider=rider, defaults={'balance': Decimal('0.00'), 'total_earned': Decimal('0.00')})

    # 2. Filter for successfully delivered or completed orders assigned to this rider for counts/commissions
    delivered_orders = Order.objects.filter(
        assigned_rider=rider, 
        status__in=[Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED]
    )

    stats = delivered_orders.aggregate(
        total_deliveries=Count('id'),
        platform_commission_total=Sum('platform_fee')
    )

    total_deliveries = stats['total_deliveries'] or 0
    platform_commission = stats['platform_commission_total'] or Decimal('0.00')
    
    # Use wallet.balance and wallet.total_earned from the database table
    rider_balance = wallet.balance
    gross_volume = rider_balance + platform_commission

    return {
        "wallet_balance": float(rider_balance),
        "total_deliveries": total_deliveries,
        "platform_commission_withheld": float(platform_commission),
        "gross_delivery_volume": float(gross_volume)
    }

@router.get("/dashboard", response=RiderDashboardResponse)
def get_rider_dashboard(request):
    user = request.user

    if not user.is_authenticated:
        raise HttpError(403, 'Forbidden')

    try:
        rider = RiderProfile.objects.select_related('user').get(user=user)
    except RiderProfile.DoesNotExist:
        raise HttpError(403, 'Forbidden: Rider profile not found')
    
    wallet_stats = get_rider_wallet_stats(rider)

    # print("Wallet Stats", wallet_stats)

    # Standard synchronous database query & evaluation
    active_deliveries = list(
        Order.objects.filter(
            assigned_rider=rider, 
            status__in=[
                Order.OrderStatus.ASSIGNED, 
                Order.OrderStatus.IN_TRANSIT
            ]
        )
        .values(
            'id', 
            'delivery_address',
            'status', 
            'package_description', 
            'marketplace_order_id',
            'receiver_name',
            'receiver_phone'
        )
    )

    return {
        "rider_name": rider.full_name,
        "status": rider.status,
        "rating": float(rider.rating),
        "is_vetted": bool(rider.is_vetted),
        "wallet": wallet_stats,
        "pending_tasks_count": len(active_deliveries),
        "active_deliveries": active_deliveries
    }


@router.post("/dashboard/status")
async def update_rider_status(request, data: RiderStatusSchema):
    if data.status not in ['available', 'busy', 'offline']:
        raise HttpError(400, "Invalid status")

    try:
        rider = await RiderProfile.objects.aget(user=request.user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    rider.status = data.status
    await rider.asave(update_fields=['status'])  # RiderProfile has no updated_at column

    # Tell the admin tracking desk that this rider changed status.
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            await channel_layer.group_send(
                "admin_tracking_desk",
                {
                    "type": "broadcast_dashboard_event",
                    "payload": {
                        "event": "rider_status_change",
                        "rider_id": str(rider.id),
                        "new_status": rider.status,
                    },
                },
            )
    except Exception:
        logger.exception("Rider status broadcast failed for rider %s", rider.id)

    return {"status": "success", "new_status": rider.status}


@router.get("/available-orders", response={200: ActiveOrdersListSchema})
def get_available_orders(request):
    # 1. Guardrail
    if request.user.role != 'rider':
        raise HttpError(403, "Access restricted.")

    # 2. Resolve the rider's assigned regional hub
    rider_location = getattr(request.user, 'base_location', None)
    rider_location_id = getattr(request.user, 'base_location_id', None)

    if not rider_location_id:
        return 200, {"status": "success", "active_orders": []}

    rider_hub_name = str(rider_location.name).strip() if rider_location else ""

    # 3. Restrict orders strictly to the rider's regional hub
    region_filter = Q(marketplace_order__vendor__user__base_location_id=rider_location_id)

    # Direct logistics orders: check whether sender points to CustomUser or a Profile with .user
    sender_field = Order._meta.get_field('sender')
    sender_model = sender_field.related_model
    if _has_model_field(sender_model, 'base_location'):
        region_filter |= Q(marketplace_order__isnull=True, sender__base_location_id=rider_location_id)
    elif _has_model_field(sender_model, 'user'):
        region_filter |= Q(marketplace_order__isnull=True, sender__user__base_location_id=rider_location_id)

    if _has_model_field(Order, 'base_location'):
        region_filter |= Q(base_location_id=rider_location_id)
    elif rider_hub_name:
        region_filter |= Q(
            marketplace_order__isnull=True,
            pickup_address__icontains=rider_hub_name
        )

    queryset = (
        Order.objects.select_related(
            'marketplace_order',
            'marketplace_order__vendor__user__base_location',
            'sender'
        )
        .filter(
            status=Order.OrderStatus.ACKNOWLEDGED,
            assigned_rider__isnull=True
        )
        .filter(region_filter)
        .distinct()
        .order_by('-created_at')
    )

    orders_list = []

    for order in queryset:
        m_id = order.marketplace_order_id if order.marketplace_order_id else "N/A"

        orders_list.append({
            "id": order.id,
            "order_reference": order.order_reference,
            "marketplace_id": str(m_id),
            "sender_name": order.sender_name,
            "pickup_address": order.pickup_address,
            "receiver_name": order.receiver_name,
            "delivery_address": order.delivery_address,
            "package_description": order.package_description or "Standard Package",
            "delivery_fee": float(order.delivery_fee),
            "status": "pending",
            "created_at": order.created_at
        })

    return 200, {"status": "success", "active_orders": orders_list}

# 1. Create a synchronous helper function for the transaction
def assign_order_transaction(mkt_id: str, rider_id: str):
    """
    Atomically claims an ACKNOWLEDGED waybill for a rider. The waybill row is locked, so
    two riders tapping "accept" at the same moment cannot both win.
    """
    with transaction.atomic():
        order = (
            Order.objects.select_for_update(of=('self',))
            .filter(marketplace_order_id=mkt_id, status=Order.OrderStatus.ACKNOWLEDGED)
            .order_by('-id')
            .first()
        )
        if order is None:
            raise Order.DoesNotExist("No acknowledged waybill is available for this order.")

        order.assigned_rider_id = rider_id
        order.status = Order.OrderStatus.ASSIGNED
        order.save(update_fields=['assigned_rider', 'status', 'updated_at'])

        MarketplaceOrder.objects.filter(
            id=mkt_id,
            status__in=[
                MarketplaceOrder.OrderStatus.PAID_UNASSIGNED,
                MarketplaceOrder.OrderStatus.PROCESSING,
            ],
        ).update(status=MarketplaceOrder.OrderStatus.PROCESSING)

        return order
    
@router.post("/accept-order/{marketplace_id}")
def accept_order(request, marketplace_id: str):
    try:
        rider = RiderProfile.objects.get(user=request.user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    if not rider.is_vetted:
        raise HttpError(403, "Your account is pending vetting approval. You cannot accept delivery orders yet.")

    try:
        assign_order_transaction(mkt_id=marketplace_id, rider_id=rider.id)
    except Order.DoesNotExist:
        raise HttpError(404, "This order is no longer available or does not exist.")
    except ValueError as e:
        raise HttpError(400, str(e))

    return {
        "status": "success",
        "message": "Order accepted successfully."
    }

@router.post("/order/pickup/{marketplace_id}")
async def rider_mark_as_picked_up(request, marketplace_id: str):
    """
    Triggered when the rider picks up the package from the vendor. Requires the order to
    be PROCESSING and assigned to this rider, then (atomically, under a row lock) releases
    the basket value into the vendor wallet and moves both orders to IN_TRANSIT.
    """
    try:
        rider = await RiderProfile.objects.aget(user=request.user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    def _execute_pickup():
        with transaction.atomic():
            # One locked read: the lock serialises double taps and concurrent pickups.
            order = (
                MarketplaceOrder.objects.select_for_update(of=('self',))
                .select_related('vendor', 'customer', 'customer__user', 'logistics_dispatch')
                .filter(id=marketplace_id, logistics_dispatch__assigned_rider=rider)
                .first()
            )
            if order is None:
                raise MarketplaceOrder.DoesNotExist("Order not found or not assigned to you.")

            if order.status != MarketplaceOrder.OrderStatus.PROCESSING:
                raise ValueError("Order must be in processing status before it can be picked up.")

            if not process_order_revenue_split(order, force=False):
                raise ValueError("Failed to process vendor revenue split.")

            order.status = MarketplaceOrder.OrderStatus.IN_TRANSIT
            order.save(update_fields=['status'])

            dispatch = order.logistics_dispatch
            if dispatch:
                dispatch.status = Order.OrderStatus.IN_TRANSIT
                dispatch.save(update_fields=['status', 'updated_at'])

            customer_user = order.customer.user if (order.customer and order.customer.user_id) else None
            return order.id, customer_user

    try:
        order_id_ref, customer_user = await sync_to_async(_execute_pickup, thread_sensitive=True)()
    except MarketplaceOrder.DoesNotExist:
        raise HttpError(404, "Order not found or not assigned to you.")
    except ValueError as e:
        raise HttpError(400, str(e))

    # --- Notifications run after the commit and can never fail the pickup ---
    try:
        title = "Order On The Way!"
        message = f"Your order #{order_id_ref} has been picked up by the rider and is now on the way!"

        if customer_user:
            await UserNotification.objects.acreate(
                user=customer_user,
                title=title,
                message=message,
                notification_type=UserNotification.NotificationType.ORDER_UPDATE,
            )

            channel_layer = get_channel_layer()
            if channel_layer:
                await channel_layer.group_send(
                    f"customer_{customer_user.id}",
                    {
                        "type": "order_in_transit_alert",
                        "payload": {"order_reference": str(order_id_ref), "message": message},
                    },
                )

            def _send_web_push():
                # Blocking network call: must not run on the event loop.
                from webpush import send_user_notification
                send_user_notification(
                    user=customer_user,
                    payload={
                        "head": title,
                        "body": message,
                        "icon": "/images/logo.png",
                        "url": f"/customer/orders/{order_id_ref}",
                    },
                    ttl=1000,
                )

            await sync_to_async(_send_web_push)()
    except Exception:
        logger.exception("Failed to dispatch customer transit notifications for order %s", order_id_ref)

    return {
        "status": "success",
        "message": "Package picked up successfully. Status transitioned to in_transit and vendor wallet credited."
    }

@router.post("/complete-delivery/{mkt_id}")
async def complete_delivery(request, mkt_id: str):
    """
    Rider marks the package as delivered (waybill -> DELIVERED). The marketplace order stays
    IN_TRANSIT until the customer confirms receipt (it used to be written as 'delivered',
    which is not a valid MarketplaceOrder status). Only a package that was actually picked
    up (IN_TRANSIT) can be delivered, so the vendor payout at pickup cannot be bypassed.
    """
    try:
        rider = await RiderProfile.objects.aget(user=request.user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    def _mark_delivered():
        with transaction.atomic():
            order = (
                Order.objects.select_for_update(of=('self',))
                .select_related('marketplace_order')
                .get(
                    marketplace_order_id=mkt_id,
                    assigned_rider=rider,
                    status=Order.OrderStatus.IN_TRANSIT,
                )
            )
            order.status = Order.OrderStatus.DELIVERED
            order.save(update_fields=['status', 'updated_at'])
            m_order = order.marketplace_order
            return str(m_order.id), m_order.vendor_id

    try:
        order_id_ref, vendor_id = await sync_to_async(_mark_delivered, thread_sensitive=True)()
    except Order.DoesNotExist:
        raise HttpError(404, "Order not found, not assigned to you, or not picked up yet.")

    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            await channel_layer.group_send(
                "admin_notifications",
                {
                    "type": "order_completed_alert",
                    "payload": {
                        "order_reference": order_id_ref,
                        "message": f"Rider {rider.first_name} marked Order #{order_id_ref} as delivered. Awaiting customer confirmation.",
                    },
                },
            )
            # Vendor dashboards listen on vendor_<MerchantProfile.id> (not the user id).
            await channel_layer.group_send(
                f"vendor_{vendor_id}",
                {
                    "type": "order_completed_alert",
                    "payload": {
                        "order_reference": order_id_ref,
                        "message": f"Your order #{order_id_ref} has been delivered by the rider. Awaiting customer sign-off.",
                    },
                },
            )
    except Exception:
        logger.exception("Delivery broadcast failed for order %s", order_id_ref)

    return {"status": "success", "message": "Delivery marked as delivered. Awaiting customer confirmation."}

def _resolve_bank_account_name(account_number: str, bank_code: str) -> str:
    """Asks Paystack who owns the account. The client-supplied account_name is never trusted."""
    try:
        response = httpx.get(
            "https://api.paystack.co/bank/resolve",
            params={"account_number": account_number, "bank_code": bank_code},
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            timeout=10.0,
        )
        body = response.json()
    except (httpx.RequestError, ValueError):
        raise HttpError(503, "Bank verification service is unavailable. Please try again shortly.")
    if response.status_code == 200 and body.get("status"):
        return body["data"]["account_name"]
    raise HttpError(400, "Could not verify this bank account.")


@router.get("/preferences")
def get_rider_preferences(request):
    """Current payout schedule/method and the linked settlement account (used by the settings page)."""
    if request.user.role != 'rider':
        raise HttpError(403, "Access restricted.")

    rider = RiderProfile.objects.select_related('settlement_account').filter(user=request.user).first()
    if rider is None:
        raise HttpError(404, "Rider profile not found.")

    account = getattr(rider, 'settlement_account', None)  # reverse one-to-one: missing row -> None
    return {
        "payout_schedule": rider.payout_schedule,
        "payout_method": rider.payout_method,
        "settlement_account": {
            "bank_name": account.bank_name,
            "bank_code": account.bank_code,
            "account_number": account.account_number,
            "account_name": account.account_name,
            "is_verified": account.is_verified,
        } if account else None,
    }


@router.post("/preferences")
def update_rider_preferences(request, payload: RiderPreferencesSchema):
    if request.user.role != 'rider':
        raise HttpError(403, "Access restricted.")

    if payload.payout_schedule not in ('daily', 'weekly'):
        raise HttpError(400, "Invalid payout schedule.")
    if payload.payout_method not in ('cash', 'account'):
        raise HttpError(400, "Invalid payout method.")

    try:
        rider = RiderProfile.objects.get(user=request.user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    resolved_name = None
    if payload.payout_method == 'account':
        existing = RiderPaymentAccount.objects.filter(rider=rider).first()
        number = (payload.account_number or (existing.account_number if existing else "") or "").strip()
        code = "".join(ch for ch in (payload.bank_code or (existing.bank_code if existing else "") or "") if ch.isdigit())
        if len(number) != 10 or not number.isdigit() or not code:
            raise HttpError(400, "A valid 10-digit account number and bank are required.")
        # Network call happens before opening the transaction.
        resolved_name = _resolve_bank_account_name(number, code)

    with transaction.atomic():
        rider.payout_schedule = payload.payout_schedule
        rider.payout_method = payload.payout_method
        rider.save(update_fields=['payout_schedule', 'payout_method'])

        if payload.payout_method == 'account':
            account, _created = RiderPaymentAccount.objects.get_or_create(
                rider=rider,
                defaults={'bank_name': payload.bank_name or '', 'account_number': number,
                          'account_name': resolved_name},
            )
            account.bank_name = payload.bank_name or account.bank_name
            account.bank_code = code
            account.account_number = number
            account.account_name = resolved_name      # from Paystack, not from the client
            account.is_verified = True                # verified by the gateway lookup above
            account.save()

    return {"status": "success", "message": "Payout preferences updated successfully."}
