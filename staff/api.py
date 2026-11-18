import json
import logging
from django.shortcuts import get_object_or_404
from ninja import Router, Query, File, Form
from django.conf import settings
from ninja.files import UploadedFile
from ninja.errors import HttpError
from django.db import transaction
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, Count, OuterRef, Exists
from admins.schemas import (
    AdminDashboardStatsSchema, 
    AdminVendorDashboardOut,
    SingleVendorResponseOut,
)
from order.models import Order
from merchant.models import MerchantProfile
from marketplace.models import MarketplaceOrder, VariantOption, ProductItem, ProductImage
from merchant.schemas import ProductDetailPayloadSchema
from customers.models import IndividualProfile, FrequentlyAskedQuestion
from customers.schemas import FAQSchema
from rider.models import RiderProfile
from accounts.models import CustomUser, CustomerReview
from .schemas import (
    ProductUpdateSchema, ReassignOrderSchema, CancelOrderSchema, VendorOrdersResponseSchema
)
from publics.models import NewsletterSubscriber, NewsletterBroadcast
from publics.schemas import NewsletterBroadcastSchema
from publics.newsletter import send_newsletter
from marketplace.utils import cancel_order_before_pickup
from audit.models import OrderDisputeRefund
from accounts.utils import JWTAuth
from typing import Optional
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from marketplace.models import Product
from messaging.models import Conversation, Message
from support.schemas import ReplySchema
from accounts.security import RequireRole

User = get_user_model()

logger = logging.getLogger("aika.accounts")


# Administrators use the same vendor/order tooling; every endpoint already scopes plain staff
# to their assigned merchants and gives admins/superusers the full view.
router = Router(tags=["Agent Management"], auth=RequireRole(allowed_roles=['staff', 'admin']))


@router.get("/dashboard-stats", response=AdminDashboardStatsSchema)
async def staff_dashboard_stats_api(request):
    """
    Returns aggregated dashboard statistics and chart trends restricted strictly 
    to the vendors explicitly assigned to the logged-in staff/agent.
    """
    user = request.auth

    # Scope Querysets Strictly to Assigned Merchants (unless superuser/admin)
    if getattr(user, 'role', None) == CustomUser.Role.STAFF and not user.is_superuser:
        merchants_qs = MerchantProfile.objects.filter(assigned_agent=user)
    else:
        merchants_qs = MerchantProfile.objects.all()

    orders_qs = Order.objects.filter(sender__in=merchants_qs)
    marketplace_orders_qs = MarketplaceOrder.objects.filter(vendor__in=merchants_qs)
    riders_qs = RiderProfile.objects.all()

    # Aggregate Metrics Using Async Queryset Methods
    total_orders = await orders_qs.acount()
    total_merchants = await merchants_qs.acount()
    total_riders = await riders_qs.acount()
    total_customers = await IndividualProfile.objects.acount()

    pending_dispatches_count = await orders_qs.filter(status='acknowledged', assigned_rider__isnull=True).acount()
    available_riders_count = await riders_qs.filter(status=RiderProfile.Status.AVAILABLE).acount()
    completed_deliveries_count = await orders_qs.filter(status='delivered').acount()

    # DYNAMIC CHART TRENDS AGGREGATION (Past 7 Days Scoped to Agent Vendors)
    today = timezone.now().date()
    start_date = today - timedelta(days=6)
    
    recent_trend_qs = marketplace_orders_qs.filter(
        created_at__date__gte=start_date
    ).annotate(
        order_date=TruncDate('created_at')
    ).values('order_date').annotate(
        daily_revenue=Sum('total_price'),
        daily_count=Count('id')
    ).order_by('order_date')

    trend_map = {}
    async for entry in recent_trend_qs:
        d_str = str(entry['order_date'])
        trend_map[d_str] = {
            "revenue": float(entry['daily_revenue'] or 0.0),
            "count": int(entry['daily_count'] or 0)
        }

    days_labels = []
    days_revenue = []
    days_volumes = []

    for i in range(7):
        current_d = start_date + timedelta(days=i)
        d_str = str(current_d)
        days_labels.append(current_d.strftime('%a'))
        
        data_point = trend_map.get(d_str, {"revenue": 0.0, "count": 0})
        days_revenue.append(data_point["revenue"])
        days_volumes.append(data_point["count"])

    return {
        "metrics": {
            "total_orders": total_orders,
            "total_merchants": total_merchants,
            "total_riders": total_riders,
            "total_customers": total_customers,
            "pending_dispatches_count": pending_dispatches_count,
            "available_riders_count": available_riders_count,
            "completed_deliveries_count": completed_deliveries_count
        },
        "chart_trends": {
            "days_labels": days_labels,
            "days_revenue": days_revenue,
            "days_volumes": days_volumes
        }
    }


@router.get("/vendors", response=AdminVendorDashboardOut)
async def staff_vendors_view(request, type: Optional[str] = Query(default=None)):
    """
    Agent workspace to audit and oversee corporate business entities explicitly assigned to them.
    """
    user = request.auth

    @sync_to_async
    def get_agent_dashboard_data():
        vendor_qs = MerchantProfile.objects.filter(business_type__isnull=False)

        if getattr(user, 'role', None) == CustomUser.Role.STAFF and not user.is_superuser:
            vendor_qs = vendor_qs.filter(assigned_agent=user)

        if type and type in MerchantProfile.BusinessType.values:
            vendor_qs = vendor_qs.filter(business_type=type)

        vendors = list(vendor_qs.select_related('user', 'assigned_agent').order_by('company_name'))
        
        if getattr(user, 'role', None) == CustomUser.Role.STAFF and not user.is_superuser:
            staff_agents = [user]
        else:
            staff_agents = list(CustomUser.objects.filter(role=CustomUser.Role.STAFF, is_active=True))

        return vendors, staff_agents

    vendors, staff_agents = await get_agent_dashboard_data()

    return {
        "vendors": vendors,
        "current_filter": type,
        "staff_agents": staff_agents
    }


@router.get("/vendor/{vendor_id}/orders", response={200: VendorOrdersResponseSchema})
async def get_staff_vendor_orders(request, vendor_id: str):
    """
    Retrieves the live orders queue and available delivery riders pool for a specific 
    merchant node with N+1 optimizations.
    """
    user = request.auth

    @sync_to_async
    def fetch_orders_and_riders():
        user_role = getattr(user, 'role', None)
        if user_role == "staff" and not user.is_superuser:
            vendor = get_object_or_404(MerchantProfile, id=vendor_id, assigned_agent=user)
        else:
            vendor = get_object_or_404(MerchantProfile, id=vendor_id)

        # Optimized with select_related to prevent N+1 queries on foreign keys
        orders_qs = Order.objects.select_related(
            'assigned_rider', 
            'assigned_rider__user', 
            'receiver', 
            'receiver__user'
        ).filter(
            sender=vendor
        ).order_by('-created_at')

        orders_data = []
        for order in orders_qs:
            rider_info = None
            if order.assigned_rider:
                rider_user = getattr(order.assigned_rider, 'user', None)
                rider_name = f"{rider_user.first_name} {rider_user.last_name}".strip() if rider_user else "Assigned Rider"
                rider_info = {
                    "id": str(order.assigned_rider.id),
                    "name": rider_name,
                    "phone": getattr(rider_user, 'phone_number', '') if rider_user else ''
                }

            customer_name = order.receiver_name or "Valued Customer"
            if hasattr(order, 'receiver') and order.receiver and hasattr(order.receiver, 'user') and order.receiver.user:
                r_user = order.receiver.user
                customer_name = f"{r_user.first_name} {r_user.last_name}".strip() or customer_name

            orders_data.append({
                "id": str(order.id),
                "customer_name": customer_name,
                "total_amount": float(getattr(order, 'delivery_fee', 0) or 0),
                "status": str(order.status).lower(),
                "assigned_rider": rider_info,
                "created_at": order.created_at.isoformat() if order.created_at else "",
                "kind": "waybill",
                "fulfillment_mode": "platform_dispatch",
                "can_cancel": order.status in (
                    Order.OrderStatus.PENDING, Order.OrderStatus.ACKNOWLEDGED, Order.OrderStatus.ASSIGNED
                ),
            })

        # Vendor-delivered orders have no waybill, so they were invisible here (and could not be
        # cancelled). List the ones that are past payment.
        vendor_delivered = (
            MarketplaceOrder.objects
            .filter(vendor=vendor, fulfillment_mode=MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY)
            .exclude(status=MarketplaceOrder.OrderStatus.PENDING)
            .select_related('customer__user')
            .order_by('-created_at')
        )
        for m_order in vendor_delivered:
            c_user = m_order.customer.user if (m_order.customer and m_order.customer.user_id) else None
            orders_data.append({
                "id": str(m_order.id),
                "customer_name": ((f"{c_user.first_name} {c_user.last_name}".strip()) if c_user else "") or "Valued Customer",
                "total_amount": float(m_order.total_price or 0),
                "status": str(m_order.status).lower(),
                "assigned_rider": None,
                "created_at": m_order.created_at.isoformat() if m_order.created_at else "",
                "kind": "vendor_delivery",
                "fulfillment_mode": m_order.fulfillment_mode,
                "can_cancel": m_order.status in (
                    MarketplaceOrder.OrderStatus.PAID_UNASSIGNED, MarketplaceOrder.OrderStatus.PROCESSING
                ),
            })

        riders_qs = RiderProfile.objects.select_related('user').filter(is_active=True)
        riders_data = []
        for rider in riders_qs:
            r_user = getattr(rider, 'user', None)
            r_name = f"{r_user.first_name} {r_user.last_name}".strip() if r_user else "Delivery Rider"
            riders_data.append({
                "id": str(rider.id),
                "name": r_name,
                "phone": getattr(r_user, 'phone_number', '') if r_user else ''
            })

        return orders_data, riders_data

    orders_list, riders_pool = await fetch_orders_and_riders()
    
    return 200, {
        "status": "success",
        "orders": orders_list,
        "riders": riders_pool
    }


@router.get("/vendor/{vendor_id}", response={200: SingleVendorResponseOut, 403: dict, 404: dict})
async def get_staff_single_vendor_detail(request, vendor_id: str):
    """
    Retrieve individual vendor details for an agent with security constraints.
    """
    user = request.auth

    @sync_to_async
    def fetch_agent_vendor_data():
        qs = MerchantProfile.objects.select_related('user', 'assigned_agent')
        
        if getattr(user, 'role', None) == CustomUser.Role.STAFF and not user.is_superuser:
            vendor = qs.get(id=vendor_id, assigned_agent=user)
        else:
            vendor = qs.get(id=vendor_id)

        staff_agents = list(CustomUser.objects.filter(role=CustomUser.Role.STAFF, is_active=True))
        return vendor, staff_agents

    try:
        vendor, staff_agents = await fetch_agent_vendor_data()
    except MerchantProfile.DoesNotExist:
        return 404, {"detail": "Vendor entity not found or unassigned to your agent profile."}

    return 200, {
        "vendor": vendor,
        "staff_agents": staff_agents
    }


@router.get("/vendor/{vendor_id}/products")
async def staff_list_vendor_products(request, vendor_id: str):
    """
    Asynchronously queries all catalog profiles owned by a specific vendor.
    """
    user = request.auth

    @sync_to_async
    def fetch_vendor_products():
        if getattr(user, 'role', None) == "staff" and not user.is_superuser:
            vendor = MerchantProfile.objects.get(id=vendor_id, assigned_agent=user)
        else:
            vendor = MerchantProfile.objects.get(id=vendor_id)

        has_variants_query = VariantOption.objects.filter(product=OuterRef('pk'))
        
        products_queryset = Product.objects.filter(vendor=vendor)\
                                           .annotate(has_variants=Exists(has_variants_query))\
                                           .select_related('sub_category')\
                                           .prefetch_related('items')\
                                           .order_by('-id')

        products_list = []
        for product in products_queryset:
            sku_records = [
                {
                    "id": sku.id,
                    "stock_qty": sku.stock_qty,
                    "price_override": float(sku.price_override) if sku.price_override else None,
                    "is_active": sku.is_active
                }
                for sku in product.items.all()
            ]

            category_label = "E-Commerce"
            if product.sub_category:
                category_label = product.sub_category.get_primary_category_display()

            base_host = f"{request.scheme}://{request.get_host()}"
            image_url = f"{base_host}{product.image.url}" if product.image else None

            products_list.append({
                "id": product.id,
                "name": product.name,
                "price": float(product.price),
                "is_available": product.is_available,
                "category": category_label,
                "bonanza_mode": product.bonanza_mode,
                "discount_price": float(product.discount_price) if product.discount_price else None,
                "image": image_url,
                "has_variants": product.has_variants,
                "sku_items": sku_records
            })

        return vendor.company_name, products_list

    try:
        company_name, products = await fetch_vendor_products()
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Vendor entity not found or unassigned to your agent profile.")

    return {
        "status": "success",
        "message": f"Successfully loaded {len(products)} asset tracks for {company_name}.",
        "products": products
    }


@router.put("/vendor/{vendor_id}/products/{product_id}")
async def staff_update_vendor_product_api(
    request, 
    vendor_id: str, 
    product_id: str, 
    payload: ProductUpdateSchema = Form(...),
    image: Optional[UploadedFile] = File(None)
):
    """
    Asynchronously processes updates for a vendor product under agent purview.
    """
    user = request.auth

    try:
        result_data = await sync_to_async(_sync_agent_update_product_worker)(
            user, vendor_id, product_id, payload, image, request
        )
        return {"status": "success", "message": "Product updated successfully.", "product": result_data}

    except Product.DoesNotExist:
        raise HttpError(404, "Product not found or unassigned to your agent profile.")
    except Exception as e:
        logger.exception(f"Failed to update agent product trace: {product_id}")
        raise HttpError(500, "Unable to execute product updates.")


def _sync_agent_update_product_worker(user, vendor_id, product_id, payload, image, request):
    with transaction.atomic():
        if getattr(user, 'role', None) == "staff" and not user.is_superuser:
            vendor = MerchantProfile.objects.get(id=vendor_id, assigned_agent=user)
        else:
            vendor = MerchantProfile.objects.get(id=vendor_id)

        product = Product.objects.get(id=product_id, vendor=vendor)
        update_data = payload.dict(exclude_unset=True)
        
        sub_category_id = update_data.pop('sub_category_id', None)
        if sub_category_id:
            product.sub_category_id = sub_category_id

        for attr, value in update_data.items():
            if hasattr(product, attr) and attr not in ['variant_options', 'sku_items', 'gallery_images']:
                setattr(product, attr, value)

        if image:
            product.image = image

        product.save()

        base_host = f"{request.scheme}://{request.get_host()}"
        primary_image_url = f"{base_host}{product.image.url}" if product.image else None

        return {
            "id": product.id,
            "name": product.name,
            "price": float(product.price),
            "image": primary_image_url,
            "is_available": product.is_available,
            "bonanza_mode": product.bonanza_mode,
            "discount_price": float(product.discount_price) if product.discount_price else None
        }


@router.post("/order/{order_id}/reassign")
async def staff_reassign_order(request, order_id: str, data: ReassignOrderSchema):
    """
    Reassigns an acknowledged/assigned delivery to another vetted, active rider.
    (The old code wrote status 'dispatched', which is not a valid order status, to ANY order
    including delivered and cancelled ones.)
    """
    user = request.auth

    @sync_to_async
    def execute_reassignment():
        with transaction.atomic():
            try:
                order = (
                    Order.objects.select_for_update(of=('self',))
                    .select_related('sender')
                    .get(id=order_id)
                )
            except (Order.DoesNotExist, ValueError):
                raise HttpError(404, "Order trace not found.")

            user_role = getattr(user, 'role', None)
            if user_role == "staff" and not user.is_superuser:
                if order.sender is None or order.sender.assigned_agent_id != user.id:
                    raise HttpError(403, "Order does not belong to your assigned merchants.")

            if order.status not in (Order.OrderStatus.ACKNOWLEDGED, Order.OrderStatus.ASSIGNED):
                raise HttpError(
                    400, "Only acknowledged or assigned orders can be reassigned to a rider."
                )

            try:
                rider = RiderProfile.objects.select_related('user').get(id=data.rider_id)
            except RiderProfile.DoesNotExist:
                raise HttpError(404, "Target rider profile not found.")
            if not (rider.is_active and rider.is_vetted):
                raise HttpError(400, "The selected rider is not active and vetted.")

            order.assigned_rider = rider
            order.status = Order.OrderStatus.ASSIGNED
            order.save(update_fields=['assigned_rider', 'status', 'updated_at'])

            return rider.user.first_name if rider.user_id else "Rider"

    rider_name = await execute_reassignment()
    return {
        "status": "success",
        "message": f"Order successfully reassigned to {rider_name}."
    }


@router.post("/order/{order_id}/cancel-refund")
async def staff_cancel_refund_order(request, order_id: str, data: CancelOrderSchema):
    """
    Cancels an order that has not been picked up / sent out yet and opens the refund workflow.

    `order_id` is a logistics order id (digits) for rider deliveries, or a marketplace order id
    for vendor-delivered orders (which have no waybill). Only unpaid/paid-but-not-dispatched
    orders can be cancelled: the vendor and rider have been paid once a delivery is under way.
    Reserved stock is restored, applied wallet credit is refunded and, for a PAID order, a refund
    request is queued for finance.
    """
    user = request.auth

    @sync_to_async
    def execute_cancellation():
        with transaction.atomic():
            user_role = getattr(user, 'role', None)
            scoped_staff = user_role == "staff" and not user.is_superuser

            # ---- vendor-delivered order (marketplace id) ----
            if not str(order_id).isdigit():
                m_order = (
                    MarketplaceOrder.objects.select_related('vendor')
                    .filter(id=order_id, fulfillment_mode=MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY)
                    .first()
                )
                if m_order is None:
                    raise HttpError(404, "Order trace not found.")
                if scoped_staff and (m_order.vendor is None or m_order.vendor.assigned_agent_id != user.id):
                    raise HttpError(403, "Unauthorized access to order.")

                cancelled, was_paid = cancel_order_before_pickup(m_order.id)
                if cancelled is None:
                    raise HttpError(400, "This order can no longer be cancelled (it is already out for delivery or finished).")
                return _queue_refund_request(cancelled, was_paid, data, user)

            # ---- rider-delivered order (logistics id) ----
            try:
                order = (
                    Order.objects.select_for_update(of=('self',))
                    .select_related('sender')
                    .get(id=int(order_id))
                )
            except (Order.DoesNotExist, ValueError):
                raise HttpError(404, "Order trace not found.")

            if scoped_staff and (order.sender is None or order.sender.assigned_agent_id != user.id):
                raise HttpError(403, "Unauthorized access to order.")

            if order.status not in (
                Order.OrderStatus.PENDING, Order.OrderStatus.ACKNOWLEDGED, Order.OrderStatus.ASSIGNED
            ):
                raise HttpError(400, f"An order in '{order.status}' status can no longer be cancelled.")

            if order.marketplace_order_id:
                cancelled, was_paid = cancel_order_before_pickup(order.marketplace_order_id)
                if cancelled is None:
                    raise HttpError(400, "This order can no longer be cancelled.")
                return _queue_refund_request(cancelled, was_paid, data, user)

            order.status = Order.OrderStatus.CANCELLED
            order.save(update_fields=['status', 'updated_at'])
            return False

    refund_queued = await execute_cancellation()
    return {
        "status": "success",
        "message": (
            "Order cancelled and a refund request was queued for finance."
            if refund_queued else "Order cancelled."
        ),
    }


@router.post("/vendor/{vendor_id}/toggle-suspension")
async def staff_toggle_vendor_suspension(request, vendor_id: str):
    """
    Toggles the store suspension status for an assigned merchant.
    """
    user = request.auth

    @sync_to_async
    def toggle_status():
        if getattr(user, 'role', None) == "staff" and not user.is_superuser:
            vendor = MerchantProfile.objects.get(id=vendor_id, assigned_agent=user)
        else:
            vendor = MerchantProfile.objects.get(id=vendor_id)
        
        current_state = getattr(vendor, 'is_active_store', True)
        vendor.is_active_store = not current_state
        vendor.save()
        return vendor.is_active_store

    try:
        new_state = await toggle_status()
        state_label = "active" if new_state else "suspended"
        return {
            "status": "success",
            "message": f"Store operational status updated to {state_label}."
        }
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Vendor not found or unauthorized.")


# ##############################################################################
# CUSTOMER REVIEW & FAQ MANAGEMENT
# ###############################################################################
@router.get("/reviews")
def staff_list_customer_reviews(request):

    reviews = CustomerReview.objects.all().order_by('-created_at')
    
    return {
        "reviews": [
            {
                "id": r.id,
                "name": r.name,
                "role": r.role,
                "comment": r.comment,
                "is_approved": r.is_approved,
                "created_at": r.created_at.isoformat()
            } for r in reviews
        ]
    }


@router.post("/reviews/{review_id}/approve")
def staff_approve_review(request, review_id: int):
    review = get_object_or_404(CustomerReview, id=review_id)
    review.is_approved = True
    review.save(update_fields=['is_approved'])
    return {"status": "success", "message": "Review approved successfully."}


@router.delete("/reviews/{review_id}/delete")
def staff_delete_review(request, review_id: int):
    review = get_object_or_404(CustomerReview, id=review_id)
    review.delete()
    return {"status": "success", "message": "Review deleted successfully."}


@router.get("/faqs")
def staff_list_faqs(request):
    faqs = FrequentlyAskedQuestion.objects.all().order_by('order', '-created_at')
    return {
        "faqs": [
            {
                "id": f.id,
                "question": f.question,
                "answer": f.answer,
                "order": f.order,
                "is_active": f.is_active
            } for f in faqs
        ]
    }


@router.post("/faqs")
def staff_create_faq(request, payload: FAQSchema):
    faq = FrequentlyAskedQuestion.objects.create(
        question=payload.question,
        answer=payload.answer,
        order=payload.order,
        is_active=payload.is_active
    )
    return {"status": "success", "message": "FAQ created successfully.", "id": faq.id}


@router.delete("/faqs/{faq_id}/delete")
def staff_delete_faq(request, faq_id: int):
    faq = get_object_or_404(FrequentlyAskedQuestion, id=faq_id)
    faq.delete()
    return {"status": "success", "message": "FAQ deleted successfully."}


@router.get("/newsletter/subscribers")
def staff_list_subscribers(request):
    subscribers = NewsletterSubscriber.objects.filter(is_active=True).order_by('-subscribed_at')
    return {
        "subscribers": [
            {
                "id": s.id,
                "email": s.email,
                "subscribed_at": s.subscribed_at.isoformat()
            } for s in subscribers
        ]
    }


@router.post("/newsletter/broadcast")
def staff_broadcast_newsletter(request, payload: NewsletterBroadcastSchema):
    user = request.auth

    try:
        sent_count, total_recipients = send_newsletter(
            payload.subject, payload.content, payload.template_type
        )
    except ValueError as e:
        raise HttpError(400, str(e))

    # The old code passed an invalid `additional_headers` argument to send_mail; the resulting
    # TypeError was swallowed and the broadcast was still recorded as sent to everyone.
    if sent_count == 0:
        raise HttpError(500, "Failed to send the newsletter. Please try again.")

    NewsletterBroadcast.objects.create(
        subject=payload.subject,
        content=payload.content,
        template_type=payload.template_type,
        sent_by=user,
        recipient_count=sent_count
    )

    return {
        "status": "success",
        "message": f"Newsletter broadcasted to {sent_count} of {total_recipients} subscribers successfully.",
    }


# ############################################
# ######### Support ##########################
# ############################################
@router.get("/queues")
def get_staff_queues(request):
    user = request.auth

    allocated_vendors = MerchantProfile.objects.filter(assigned_agent=user)

    unassigned_vendor_chats = Conversation.objects.filter(
        conversation_type='vendor_support',
        vendor_profile__in=allocated_vendors,
        assigned_staff__isnull=True,
        is_active=True
    ).select_related('vendor_profile', 'customer').order_by('-created_at')

    my_active_vendor_chats = Conversation.objects.filter(
        conversation_type='vendor_support',
        vendor_profile__in=allocated_vendors,
        assigned_staff=user,
        is_active=True
    ).select_related('vendor_profile', 'customer').order_by('-updated_at')

    return {
        "status": "success",
        "unassigned_queue": [
            {
                "id": c.id,
                "vendor_name": c.vendor_profile.company_name if c.vendor_profile else 'Unknown Vendor',
                "vendor_id": c.vendor_profile.id if c.vendor_profile else None,
                "subject": c.subject,
                "created_at": c.created_at.isoformat()
            } for c in unassigned_vendor_chats
        ],
        "my_active_chats": [
            {
                "id": c.id,
                "vendor_name": c.vendor_profile.company_name if c.vendor_profile else 'Unknown Vendor',
                "vendor_id": c.vendor_profile.id if c.vendor_profile else None,
                "subject": c.subject,
                "updated_at": c.updated_at.isoformat()
            } for c in my_active_vendor_chats
        ]
    }


@router.post("/chat/{conv_id}/join")
def join_vendor_chat(request, conv_id: str):
    user = request.auth

    with transaction.atomic():
        conv = Conversation.objects.select_for_update().select_related('vendor_profile').get(
            id=conv_id, 
            conversation_type='vendor_support'
        )

        if not conv.vendor_profile or conv.vendor_profile.assigned_agent != user:
            raise HttpError(403, "You are not allocated to support this vendor.")

        if conv.assigned_staff and conv.assigned_staff != user:
            raise HttpError(400, "This vendor conversation has already been claimed by another staff member.")

        conv.assigned_staff = user
        conv.save(update_fields=['assigned_staff'])

        Message.objects.create(
            conversation=conv,
            sender=user,
            content=f"Support Staff {user.email} has joined the vendor conversation."
        )

    return {"status": "success", "message": "Successfully joined vendor conversation."}


@router.post("/chat/{conv_id}/reply")
def staff_send_vendor_message(request, conv_id: str, payload: ReplySchema):
    user = request.auth

    conv = get_object_or_404(
        Conversation, 
        id=conv_id, 
        conversation_type='vendor_support', 
        assigned_staff=user
    )

    if not conv.is_active:
        raise HttpError(400, "Cannot reply to a closed conversation.")

    message = Message.objects.create(
        conversation=conv,
        sender=user,
        content=payload.content.strip()
    )
    
    conv.save(update_fields=['updated_at'])

    return {
        "status": "success",
        "message": {
            "id": message.id,
            "sender_email": user.email,
            "content": message.content,
            "created_at": message.created_at.isoformat()
        }
    }

def _queue_refund_request(m_order, was_paid, data, staff_user) -> bool:
    """Opens (once) a finance refund request for a cancelled PAID order. Returns True if one is open."""
    if not (was_paid and data.issue_refund):
        return False

    customer_user = m_order.customer.user if (m_order.customer and m_order.customer.user_id) else None
    already_open = OrderDisputeRefund.objects.filter(
        order_id=str(m_order.id), status=OrderDisputeRefund.StatusChoice.PENDING
    ).exists()
    if not already_open:
        OrderDisputeRefund.objects.create(
            order_id=str(m_order.id),
            customer_name=((customer_user.get_full_name() or customer_user.email) if customer_user else "Customer")[:150],
            reason=(data.reason or "Cancelled by agent intervention")[:2000],
            amount=m_order.total_price,
            status=OrderDisputeRefund.StatusChoice.PENDING,
            raised_by=f"Staff: {staff_user.email}"[:100],
        )
    return True
