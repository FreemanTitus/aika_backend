import logging

from asgiref.sync import sync_to_async
from django.utils import timezone
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Prefetch
from ninja import Router
from ninja.errors import HttpError

from accounts.utils import JWTAuth
from marketing.models import UserNotification
from marketplace.models import MarketplaceOrder, MarketplaceOrderItem
from marketplace.utils import create_logistics_waybill
from order.models import Order as LogisticsOrder

from .schemas import APIActionResponse
from .services import complete_marketplace_order

logger = logging.getLogger(__name__)

router = Router(tags=["Order Operations"], auth=JWTAuth())


# =========================================================================
# 1. MERCHANT: ACKNOWLEDGE ORDER API
# =========================================================================
def _commit_acknowledgment(order_id, user):
    """
    Moves a PAID order to PROCESSING under a row lock.

    Only paid orders can be acknowledged: PAID_UNASSIGNED (normal) or PROCESSING (retry / the
    post-payment step failed). PENDING (unpaid), CANCELLED, IN_TRANSIT and COMPLETED orders are
    rejected, so a merchant can neither skip payment nor drag a finished order backwards.

    Platform-delivered orders also get their waybill ACKNOWLEDGED (created now if it is missing).
    Vendor-delivered orders have no waybill and never reach the rider pool.

    Returns (logistics_ref, vendor_name, pickup_location, agent_id, customer_user, order_id,
             newly_acknowledged, is_vendor_delivery)
    """
    with transaction.atomic():
        try:
            order = (
                MarketplaceOrder.objects.select_for_update(of=('self',))
                .select_related(
                    'vendor', 'vendor__user', 'vendor__assigned_agent',
                    'customer', 'customer__user', 'logistics_dispatch',
                )
                .get(id=order_id, vendor__user=user)
            )
        except MarketplaceOrder.DoesNotExist:
            raise LookupError("Marketplace order matching your business credentials not found.")

        allowed = (
            MarketplaceOrder.OrderStatus.PAID_UNASSIGNED,
            MarketplaceOrder.OrderStatus.PROCESSING,
        )
        if order.status not in allowed:
            if order.status == MarketplaceOrder.OrderStatus.PENDING:
                raise ValueError("This order has not been paid yet and cannot be acknowledged.")
            raise ValueError(f"An order in '{order.status}' status cannot be acknowledged.")

        is_vendor_delivery = (
            order.fulfillment_mode == MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY
        )

        logistics_ref = "N/A"
        if is_vendor_delivery:
            newly_acknowledged = order.status == MarketplaceOrder.OrderStatus.PAID_UNASSIGNED
        else:
            dispatch = order.logistics_dispatch
            if dispatch is None:
                # The paid order never got its waybill (creation failed earlier): create it now.
                with transaction.atomic():
                    dispatch = create_logistics_waybill(order)

            newly_acknowledged = dispatch.status == LogisticsOrder.OrderStatus.PENDING
            if newly_acknowledged:
                dispatch.status = LogisticsOrder.OrderStatus.ACKNOWLEDGED
                dispatch.save(update_fields=['status', 'updated_at'])
            logistics_ref = dispatch.order_reference or "N/A"

        if order.status != MarketplaceOrder.OrderStatus.PROCESSING:
            order.status = MarketplaceOrder.OrderStatus.PROCESSING
            order.save(update_fields=['status'])

        vendor = order.vendor
        user_obj = getattr(vendor, 'user', None)
        pickup_location = (getattr(user_obj, 'street_address', None) or 'Merchant Location')
        agent_id = vendor.assigned_agent_id
        customer_user = order.customer.user if (order.customer and order.customer.user_id) else None

        return (
            logistics_ref,
            vendor.company_name or "Storefront Engine",
            pickup_location,
            agent_id,
            customer_user,
            order.id,
            newly_acknowledged,
            is_vendor_delivery,
        )


@router.post("/acknowledge/{order_id}", response={200: APIActionResponse})
async def acknowledge_order(request, order_id: str):
    user = request.user

    try:
        (logistics_id_string, vendor_display_name, pickup_location, agent_id,
         customer_user, mkt_id, newly_acknowledged, is_vendor_delivery) = await sync_to_async(
            _commit_acknowledgment
        )(order_id, user)
    except LookupError as e:
        raise HttpError(404, str(e))
    except ValueError as e:
        raise HttpError(400, str(e))

    # Only the first acknowledgement alerts anyone: a retry must not spam riders/customers.
    if newly_acknowledged:
        try:
            if customer_user:
                await UserNotification.objects.acreate(
                    user=customer_user,
                    title="Order Acknowledged!",
                    message=(
                        f"Your order #{mkt_id} has been acknowledged by {vendor_display_name}, which will deliver it to you."
                        if is_vendor_delivery else
                        f"Your order #{mkt_id} has been acknowledged by {vendor_display_name} and is now being prepared."
                    ),
                    notification_type=UserNotification.NotificationType.ORDER_UPDATE,
                )

            # Vendor-delivered orders never go to the rider pool or the dispatch desks.
            channel_layer = get_channel_layer()
            if channel_layer and not is_vendor_delivery:
                target_groups = ["admin_notifications", "rider_job_pool"]
                if agent_id:
                    target_groups.append(f"agent_operations_{agent_id}")

                for group_name in target_groups:
                    await channel_layer.group_send(
                        group_name,
                        {
                            "type": "send_order_alert",
                            "payload": {
                                "event": "new_available_order",
                                "order_id": str(mkt_id),
                                "logistics_ref": str(logistics_id_string),
                                "restaurant_name": str(vendor_display_name),
                                "pickup_address": str(pickup_location),
                                "is_assigned_directly": bool(agent_id),
                                "message": (
                                    f"New Action Required: {vendor_display_name} has acknowledged "
                                    f"order #{mkt_id}. Dispatch a delivery bike immediately!"
                                ),
                            },
                        },
                    )
        except Exception:
            logger.exception("Acknowledgement notification/broadcast failed for order %s", mkt_id)

    return 200, {
        "status": "success",
        "message": (
            f"Order #{mkt_id} acknowledged. You are delivering this order yourself: mark it out for delivery when it leaves."
            if is_vendor_delivery else
            f"Order #{mkt_id} verified. Production logs transmitted to your assigned manager."
        ),
        "order_id": str(mkt_id),
    }


# =========================================================================
# 2. CUSTOMER / VENDOR: CONFIRM DELIVERY RECEIVED API
# =========================================================================
@router.post("/confirm-delivery/{order_reference}", response={200: APIActionResponse})
async def confirm_delivery_received(request, order_reference: str):
    """
    Lets the receiving customer (or the sending vendor) close out a delivery by its
    logistics reference. Ownership is checked against the owning USER of the
    receiver/sender profiles. (The old code compared profile primary keys to
    user.id, which never matched, so nobody could ever confirm.)

    Completion goes through the shared, locked complete_marketplace_order(), so the
    marketplace order, waybill, rider credit and platform ledger all update together
    and exactly once.
    """
    user = request.user

    def _resolve_and_complete():
        try:
            waybill = LogisticsOrder.objects.select_related(
                'receiver', 'sender'
            ).get(order_reference=order_reference)
        except LogisticsOrder.DoesNotExist:
            raise LookupError("Delivery tracking instance not found.")

        owner_ids = {
            getattr(waybill.receiver, 'user_id', None),
            getattr(waybill.sender, 'user_id', None),
        }
        if user.id not in owner_ids or not waybill.marketplace_order_id:
            raise LookupError("Delivery tracking instance not found.")

        return complete_marketplace_order(waybill.marketplace_order_id, user, allow_vendor=True)

    try:
        m_order, _logistics, _balance, _rider_user_id = await sync_to_async(_resolve_and_complete)()
    except LookupError as e:
        raise HttpError(404, str(e))
    except ValueError as e:
        raise HttpError(400, str(e))

    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            await channel_layer.group_send(
                "admin_notifications",
                {
                    "type": "order_completed_alert",
                    "payload": {
                        "order_reference": str(order_reference),
                        "message": f"Order #{m_order.id} has been marked completed by the client.",
                    },
                },
            )
    except Exception:
        logger.exception("Completion broadcast failed for order %s", m_order.id)

    return 200, {
        "status": "success",
        "message": "Delivery successfully confirmed! Thank you for using Aika.",
        "order_reference": str(order_reference),
    }


# =========================================================================
# 3. VENDOR SELF-DELIVERY (vendor delivers the order itself)
# =========================================================================
SELF_DELIVERY_OPEN_STATES = (
    MarketplaceOrder.OrderStatus.PAID_UNASSIGNED,
    MarketplaceOrder.OrderStatus.PROCESSING,
    MarketplaceOrder.OrderStatus.IN_TRANSIT,
)


def _self_delivery_transition(order_id, user, action):
    """
    Vendor-side status changes for orders the vendor delivers itself.
      dispatch  : PROCESSING -> IN_TRANSIT   (the order left the shop)
      delivered : IN_TRANSIT  -> stamps vendor_marked_delivered_at (customer still confirms;
                  it auto-confirms after SELF_DELIVERY_AUTO_CONFIRM_HOURS)
    Only the owning vendor can do this, and only for orders in self-delivery mode.
    """
    with transaction.atomic():
        try:
            order = (
                MarketplaceOrder.objects.select_for_update(of=('self',))
                .select_related('vendor', 'customer', 'customer__user')
                .get(
                    id=order_id,
                    vendor__user=user,
                    fulfillment_mode=MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY,
                )
            )
        except MarketplaceOrder.DoesNotExist:
            raise LookupError("Self-delivery order not found for your store.")

        if action == "dispatch":
            if order.status == MarketplaceOrder.OrderStatus.IN_TRANSIT:
                raise ValueError("This order is already out for delivery.")
            if order.status != MarketplaceOrder.OrderStatus.PROCESSING:
                raise ValueError("Acknowledge the order before sending it out for delivery.")
            order.status = MarketplaceOrder.OrderStatus.IN_TRANSIT
            order.save(update_fields=['status'])
        else:  # delivered
            if order.status != MarketplaceOrder.OrderStatus.IN_TRANSIT:
                raise ValueError("Mark the order out for delivery first.")
            if order.vendor_marked_delivered_at is not None:
                raise ValueError("This order is already marked as delivered.")
            order.vendor_marked_delivered_at = timezone.now()
            order.save(update_fields=['vendor_marked_delivered_at'])

        customer_user = order.customer.user if (order.customer and order.customer.user_id) else None
        return order.id, order.vendor.company_name or "the seller", customer_user


async def _notify_customer_of_self_delivery(order_id, vendor_name, customer_user, action):
    if not customer_user:
        return
    if action == "dispatch":
        title = "Order On The Way!"
        message = f"{vendor_name} has sent out your order #{order_id} and is delivering it to you."
    else:
        title = "Please Confirm Delivery"
        message = f"{vendor_name} reports order #{order_id} as delivered. Please confirm you received it."
    try:
        await UserNotification.objects.acreate(
            user=customer_user, title=title, message=message,
            notification_type=UserNotification.NotificationType.ORDER_UPDATE,
        )
        channel_layer = get_channel_layer()
        if channel_layer:
            await channel_layer.group_send(
                f"customer_{customer_user.id}",
                {
                    "type": "order_in_transit_alert" if action == "dispatch" else "broadcast_dashboard_event",
                    "payload": {
                        "event": "order_in_transit" if action == "dispatch" else "awaiting_confirmation",
                        "order_reference": str(order_id),
                        "message": message,
                    },
                },
            )
    except Exception:
        logger.exception("Self-delivery notification failed for order %s", order_id)


@router.get("/self-delivery/active")
def list_self_delivery_orders(request):
    """The logged-in vendor's open orders that it delivers itself (they have no waybill, so they
    do not appear in the rider/logistics based active-orders list)."""
    user = request.user
    items_prefetch = Prefetch(
        'items',
        queryset=MarketplaceOrderItem.objects.select_related('product', 'product_item__product'),
    )
    orders = (
        MarketplaceOrder.objects
        .filter(
            vendor__user=user,
            fulfillment_mode=MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY,
            status__in=SELF_DELIVERY_OPEN_STATES,
        )
        .select_related('customer__user')
        .prefetch_related(items_prefetch)
        .order_by('-created_at')
    )

    result = []
    for order in orders:
        customer_user = order.customer.user if (order.customer and order.customer.user_id) else None
        result.append({
            "order_id": str(order.id),
            "status": order.status,
            "customer_name": (f"{customer_user.first_name} {customer_user.last_name}".strip() if customer_user else "") or "Customer",
            "customer_phone": (getattr(customer_user, 'phone_number', '') if customer_user else '') or '',
            "delivery_address": order.delivery_address,
            "base_location": order.base_location or '',
            "items": [
                {"name": item.get_product_name, "quantity": item.quantity,
                 "price_at_order": float(item.price_at_order)}
                for item in order.items.all()
            ],
            "subtotal": float(order.subtotal),
            "delivery_fee": float(order.delivery_fee),
            "total_price": float(order.total_price),
            "marked_delivered": order.vendor_marked_delivered_at is not None,
            "created_at": order.created_at.isoformat(),
        })
    return {"status": "success", "orders": result}


@router.post("/self-delivery/{order_id}/dispatch", response={200: APIActionResponse})
async def self_delivery_dispatch(request, order_id: str):
    try:
        mkt_id, vendor_name, customer_user = await sync_to_async(_self_delivery_transition)(
            order_id, request.user, "dispatch")
    except LookupError as e:
        raise HttpError(404, str(e))
    except ValueError as e:
        raise HttpError(400, str(e))

    await _notify_customer_of_self_delivery(mkt_id, vendor_name, customer_user, "dispatch")
    return 200, {"status": "success", "message": f"Order #{mkt_id} is now out for delivery.", "order_id": str(mkt_id)}


@router.post("/self-delivery/{order_id}/delivered", response={200: APIActionResponse})
async def self_delivery_delivered(request, order_id: str):
    try:
        mkt_id, vendor_name, customer_user = await sync_to_async(_self_delivery_transition)(
            order_id, request.user, "delivered")
    except LookupError as e:
        raise HttpError(404, str(e))
    except ValueError as e:
        raise HttpError(400, str(e))

    await _notify_customer_of_self_delivery(mkt_id, vendor_name, customer_user, "delivered")
    return 200, {
        "status": "success",
        "message": f"Order #{mkt_id} marked as delivered. Your payment is released when the customer confirms.",
        "order_id": str(mkt_id),
    }
