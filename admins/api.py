import json
import logging
from django.shortcuts import get_object_or_404
from typing import List, Optional
from django.db import IntegrityError, transaction
from ninja import Router, Query, File, Form
from ninja.files import UploadedFile
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from ninja.errors import HttpError
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.conf import settings
from datetime import datetime
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from datetime import timedelta
from django.db.models import Sum, Count, Exists, OuterRef
from .schemas import (AdminDashboardResponseSchema, 
    AdminDashboardStatsSchema, 
    AdminVendorDashboardOut,
    AssignAgentSchema,
    SingleVendorResponseOut,
    StaffOnboardingInput, StaffOnboardingResponse,
    StaffListResponseOut, ToggleApprovalSchema, SubscriptionPlanCreateSchema,
    SubscriptionPlanOutSchema, AdminProfitResponseSchema, ProductUpdateSchema,
    RiderPayoutAuditOut, AgentListResponseOut, LocationCreateInput, LocationResponse,
    SubscriptionPlanUpdateSchema, PlatformSettingCreateSchema, PlatformSettingOutSchema,
    DeliveryConfigOut, DeliveryConfigUpdateIn
)
from order.models import Order
from merchant.models import MerchantProfile
from marketplace.models import MarketplaceOrder, VariantOption, Product
from customers.models import IndividualProfile, FrequentlyAskedQuestion
from customers.schemas import FAQSchema
from staff.models import StaffProfile, AgentProfile
from rider.models import RiderProfile, RiderReview
from accounts.models import CustomUser, CustomerReview, CoverageArea, State
from payment.models import (
    SubscriptionPlan, MarketplaceTransaction, SubscriptionInvoice, VendorSubscription
)
from accounts.security import RequireRole
from core.models import PlatformSetting, DeliveryConfig
from payment.schemas import VendorSubscriptionDetailOut
from staff.schemas import ReassignOrderSchema
from accounts.utils import JWTAuth
from django.contrib.auth import get_user_model
from audit.models import SystemAuditLog, OrderDisputeRefund
from audit.schemas import AuditLogOut, DisputeOut
from audit.decorators import log_system_action
from decimal import Decimal
from publics.models import NewsletterSubscriber, NewsletterBroadcast
from publics.newsletter import send_newsletter
from marketing.models import SystemBroadcast
from publics.schemas import NewsletterBroadcastSchema, BroadcastCreateSchema
from aika_platform.models import PlatformLedger
from webpush import send_group_notification
from webpush.models import Group as WebPushGroup



User = get_user_model()

logger = logging.getLogger("aika.accounts")


# Management staff and superusers may enter this router; endpoints that are admin-only
# call _require_admin() explicitly. (A superuser-only router locked staff out of the
# dashboards, vendor, rider and dispute screens they are meant to operate.)
router = Router(tags=["Administrative Management"], auth=RequireRole(require_staff=True))


def _is_admin(user) -> bool:
    return bool(user and user.is_authenticated and (user.is_superuser or getattr(user, 'role', None) == CustomUser.Role.ADMIN))


def _require_admin(user):
    if not _is_admin(user):
        raise HttpError(403, "Administrator privileges required.")


@router.get("/dashboard", response=AdminDashboardResponseSchema)
def admin_dashboard_api(request):
    """
    Core administrative command center endpoint. Restricts access to superusers/staff
    and returns JSON data for the frontend dashboard matrix.
    """
    pending_dispatches = Order.objects.filter(
        status='acknowledged', assigned_rider__isnull=True
    ).select_related('sender', 'marketplace_source').order_by('-updated_at')

    formatted_dispatches = []
    for order in pending_dispatches:
        formatted_dispatches.append({
            "id": str(order.id),
            "status": order.status,
            "updated_at": order.updated_at,
            "sender_name": getattr(order.sender, 'name', str(order.sender)) if order.sender else None
        })

    available_riders = [rider for rider in RiderProfile.objects.filter(status=RiderProfile.Status.AVAILABLE)]

    return {
        "pending_dispatches": formatted_dispatches,
        "available_riders": available_riders
    }

@router.get("/dashboard-stats", response=AdminDashboardStatsSchema)
def admin_dashboard_stats_api(request):
    """
    Returns aggregated platform-wide statistics and dynamic time-series chart trends for the admin dashboard.
    Restricted strictly to superusers or management staff.
    """
    total_orders = Order.objects.count()
    total_merchants = MerchantProfile.objects.count()
    total_riders = RiderProfile.objects.count()
    total_customers = IndividualProfile.objects.count()

    pending_dispatches_count = Order.objects.filter(status='acknowledged', assigned_rider__isnull=True).count()
    available_riders_count = RiderProfile.objects.filter(status=RiderProfile.Status.AVAILABLE).count()
    completed_deliveries_count = Order.objects.filter(status='delivered').count()

    # =========================================================================
    # DYNAMIC CHART TRENDS AGGREGATION (Past 7 Days Platform-Wide)
    # =========================================================================
    today = timezone.now().date()
    start_date = today - timedelta(days=6)
    
    recent_trend_qs = MarketplaceOrder.objects.filter(
        created_at__date__gte=start_date
    ).annotate(
        order_date=TruncDate('created_at')
    ).values('order_date').annotate(
        daily_revenue=Sum('total_price'),
        daily_count=Count('id')
    ).order_by('order_date')

    trend_map = {}
    for entry in recent_trend_qs:
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

@router.get("/company-profit", response=AdminProfitResponseSchema)
def get_admin_company_profit_api(request):
    """
    Aggregates total admin profits from platform logistics cuts and subscription invoices, 
    returning a unified transaction audit trail.
    """
    _require_admin(request.auth)

    logistics_aggregates = PlatformLedger.objects.filter(
        source_type=PlatformLedger.RevenueSource.LOGISTICS_CUT
    ).aggregate(
        total_logistics_cut=Sum('amount')
    )
    delivery_profit = float(logistics_aggregates['total_logistics_cut'] or 0.0)

    subscription_aggregates = SubscriptionInvoice.objects.filter(
        status=SubscriptionInvoice.PaymentStatus.SUCCESSFUL
    ).aggregate(
        total_sub=Sum('amount')
    )
    subscription_profit = float(subscription_aggregates['total_sub'] or 0.0)

    total_revenue = delivery_profit + subscription_profit

    transactions_list = []

    sub_invoices_qs = SubscriptionInvoice.objects.filter(
        status=SubscriptionInvoice.PaymentStatus.SUCCESSFUL
    ).select_related('plan', 'vendor_profile').order_by('-payment_date')[:15]
    
    for inv in sub_invoices_qs:
        transactions_list.append({
            "transaction_reference": str(inv.reference_id),
            "source_type": "subscription",
            "amount": float(inv.amount),
            "description": f"Subscription plan payment ({inv.plan.name})",
            "created_at": inv.payment_date
        })

    platform_ledger_qs = PlatformLedger.objects.filter(
        source_type=PlatformLedger.RevenueSource.LOGISTICS_CUT
    ).select_related('logistics_order').order_by('-created_at')[:15]
    
    for entry in platform_ledger_qs:
        transactions_list.append({
            "transaction_reference": str(entry.reference),
            "source_type": "delivery",
            "amount": float(entry.amount),
            "description": entry.description or f"Platform logistics cut for Waybill #{entry.logistics_order_id if entry.logistics_order else 'N/A'}",
            "created_at": entry.created_at
        })

    transactions_list.sort(key=lambda x: x["created_at"], reverse=True)

    return {
        "summary": {
            "total_revenue": total_revenue,
            "delivery_profit": delivery_profit,
            "subscription_profit": subscription_profit,
            "platform_commission_profit": 0.0
        },
        "transactions": transactions_list[:30]
    }

@router.get("/vendors", response=AdminVendorDashboardOut)
def admin_vendors_view(request, type: Optional[str] = Query(default=None)):
    """
    Management dashboard workspace to audit, approve, and oversee corporate business entities.
    """
    user = request.auth  
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role not in [CustomUser.Role.ADMIN, CustomUser.Role.STAFF] and not user.is_superuser):
        raise HttpError(403, "Access unauthorized. Management credentials required.")

    vendor_qs = MerchantProfile.objects.filter(business_type__isnull=False)

    if user.role == CustomUser.Role.STAFF and not user.is_superuser:
        vendor_qs = vendor_qs.filter(assigned_agent=user)

    if type and type in MerchantProfile.BusinessType.values:
        vendor_qs = vendor_qs.filter(business_type=type)

    vendors = list(vendor_qs.select_related('user', 'assigned_agent').order_by('company_name'))
    staff_agents = list(CustomUser.objects.filter(role=CustomUser.Role.STAFF, is_active=True))

    return {
        "vendors": vendors,
        "current_filter": type,
        "staff_agents": staff_agents
    }


# ###########################################################
# Vendor Management & Assignment APIs
# ###########################################################

@router.get("/vendors/{vendor_id}", response={200: SingleVendorResponseOut, 403: dict, 404: dict})
def get_single_vendor_detail(request, vendor_id: str):
    """
    Retrieve individual vendor profile details along with active staff agents pool for auditing.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role not in [CustomUser.Role.ADMIN, CustomUser.Role.STAFF] and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Management credentials required."}

    try:
        vendor = MerchantProfile.objects.select_related('user', 'assigned_agent').get(id=vendor_id)
    except MerchantProfile.DoesNotExist:
        return 404, {"detail": "Vendor entity not found."}

    if user.role == CustomUser.Role.STAFF and not user.is_superuser and vendor.assigned_agent_id != user.id:
        return 403, {"detail": "This vendor is not assigned to you."}

    staff_agents = list(CustomUser.objects.filter(role=CustomUser.Role.STAFF, is_active=True))

    return 200, {
        "vendor": vendor,
        "staff_agents": staff_agents
    }

@router.get("/vendor/{vendor_id}/products")
def admin_list_vendor_products(request, vendor_id: str):
    """
    Queries all catalog profiles owned by a specific vendor.
    """
    user = request.auth or request.user
    if not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")

    try:
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

        company_name = vendor.company_name
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Vendor entity not found or unassigned to your agent profile.")

    return {
        "status": "success",
        "message": f"Successfully loaded {len(products_list)} asset tracks for {company_name}.",
        "products": products_list
    }

@router.put("/vendor/{vendor_id}/products/{product_id}")
def admin_update_vendor_product_api(
    request, 
    vendor_id: str, 
    product_id: str, 
    payload: ProductUpdateSchema = Form(...),
    image: Optional[UploadedFile] = File(None)
):
    """
    Processes updates for a product belonging to a vendor.
    """
    user = request.auth or request.user
    if not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")

    try:
        result_data = _sync_agent_update_product_worker(
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

@router.post("/vendors/{vendor_id}/assign", response={200: dict, 403: dict, 400: dict, 404: dict})
def update_vendor_assignment(request, vendor_id: str, payload: AssignAgentSchema):
    """
    Update or clear the assigned support agent for a given vendor entity.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role not in [CustomUser.Role.ADMIN, CustomUser.Role.STAFF] and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Management credentials required."}

    try:
        vendor = MerchantProfile.objects.get(id=vendor_id)
        agent_id = payload.assigned_agent_id
        
        if agent_id:
            agent = CustomUser.objects.get(id=agent_id, role=CustomUser.Role.STAFF)
            vendor.assigned_agent = agent
        else:
            vendor.assigned_agent = None
            
        vendor.save()
    except MerchantProfile.DoesNotExist:
        return 404, {"detail": "Vendor entity not found."}
    except CustomUser.DoesNotExist:
        return 400, {"detail": "Specified staff agent does not exist or is inactive."}

    return 200, {"success": True, "message": "Vendor assignment updated successfully."}

@router.post("/vendors/{vendor_id}/toggle-approval", response={200: dict, 400: dict, 403: dict, 404: dict})
def toggle_vendor_approval(request, vendor_id: str, payload: ToggleApprovalSchema):
    """
    Mutates verification status fields on a business node and broadcasts updates.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role not in [CustomUser.Role.ADMIN, CustomUser.Role.STAFF] and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Administrator credentials required."}

    try:
        vendor = MerchantProfile.objects.get(id=vendor_id)
    except MerchantProfile.DoesNotExist:
        return 404, {"detail": "Vendor entity not found."}

    action_type = payload.action
    channel_layer = get_channel_layer()

    with transaction.atomic():
        if action_type == 'approve':
            vendor.is_approved = True
            vendor.save()
            
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f"vendor_{vendor.id}",
                    {
                        "type": "broadcast_dashboard_event",
                        "payload": {
                            "event": "approval_update",
                            "is_approved": True,
                            "title": "Verification Approved",
                            "message": f"Congratulations! TechForge administrators have verified your business profile credentials."
                        }
                    }
                )
            return 200, {
                "success": True,
                "message": f"Corporate clearing confirmed! {vendor.company_name} has been verified.",
                "is_approved": True
            }
            
        elif action_type == 'unapprove':
            vendor.is_approved = False
            vendor.save()
            
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f"vendor_{vendor.id}",
                    {
                        "type": "broadcast_dashboard_event",
                        "payload": {
                            "event": "approval_update",
                            "is_approved": False,
                            "title": "Verification Suspended",
                            "message": "Your corporate clearance profile status has been recalled for audit reviews."
                        }
                    }
                )
            return 200, {
                "success": True,
                "message": f"Operational authority revoked. {vendor.company_name} verification status is now suspended.",
                "is_approved": False
            }
        else:
            return 400, {"detail": "Malformed pipeline payload action rejected."}
        
@router.delete("/vendors/{vendor_id}", response={200: dict, 403: dict, 404: dict})
def delete_vendor_profile(request, vendor_id: str):
    """
    Permanently purges a corporate profile anchor from the active ledger registry.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Administrator credentials required."}

    try:
        vendor = MerchantProfile.objects.get(id=vendor_id)
    except MerchantProfile.DoesNotExist:
        return 404, {"detail": "Vendor entity not found."}

    company_identifier = vendor.company_name

    try:
        with transaction.atomic():
            vendor.delete()
            return 200, {
                "success": True,
                "message": f"Data purge sequence complete. {company_identifier} has been entirely deleted from the system ledger."
            }
    except Exception as e:
        return 400, {"detail": f"Critical anomaly blocked anchor destruction sequence: {str(e)}"}

@router.post("/vendors/{vendor_id}/toggle-suspension")
@log_system_action(
    action_name=lambda res, req, vendor_id, **kw: "ACTIVATE_VENDOR" if "active" in res.get("message", "") else "SUSPEND_VENDOR",
    target_getter=lambda req, vendor_id, **kw: f"Merchant Profile #{vendor_id}"
)
def admin_toggle_vendor_suspension(request, vendor_id: str):
    """
    Allows administrators or superusers to toggle the operational store status.
    """
    user = request.auth or request.user
    if not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")

    if not (user.is_superuser or getattr(user, 'role', None) == 'admin'):
        raise HttpError(403, "Access Denied: Administrator privileges required.")

    try:
        vendor = MerchantProfile.objects.get(id=vendor_id)
        current_state = getattr(vendor, 'is_active_store', True)
        vendor.is_active_store = not current_state
        vendor.save()
        new_state = vendor.is_active_store
        company_name = vendor.company_name
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Vendor entity not found.")
    
    state_label = "active" if new_state else "suspended"
    return {
        "status": "success",
        "message": f"Store '{company_name}' operational status has been updated to {state_label}."
    }
    
# ###########################################################
# Staff Onboarding & Management APIs
# ###########################################################

@router.post("/onboard/staff", response={201: StaffOnboardingResponse, 403: dict, 400: dict})
def onboard_user(request, payload: StaffOnboardingInput):
    """
    Register a new internal user (Admin, Staff, or Support Agent) and initialize their profile.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access denied. Administrator credentials required to onboard internal users."}

    if User.objects.filter(email=payload.email).exists():
        return 400, {"detail": "Email address is already registered."}

    target_role = payload.account_role.lower()
    if target_role not in [CustomUser.Role.ADMIN, CustomUser.Role.STAFF, CustomUser.Role.AGENT]:
        return 400, {"detail": "Invalid role selection. Must be admin, staff, or agent."}

    coverage_area = None
    if payload.base_location:
        try:
            coverage_area = CoverageArea.objects.get(id=payload.base_location)
        except CoverageArea.DoesNotExist:
            return 400, {"detail": "Selected base location hub does not exist."}

    is_super = (target_role == CustomUser.Role.ADMIN)
    is_staff_flag = (target_role in [CustomUser.Role.ADMIN, CustomUser.Role.STAFF])

    created_user = User.objects.create_user(
        email=payload.email,
        password=payload.password,
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone_number=payload.phone_number,
        base_location=coverage_area,
        street_address=payload.street_address,
        role=target_role,
        is_terms_accepted=payload.is_terms_accepted,
        is_superuser=is_super,
        is_staff=is_staff_flag,
    )

    profile = None
    if target_role == CustomUser.Role.STAFF:
        profile = getattr(created_user, 'staff_profile', None)
        if profile and hasattr(payload, 'department') and payload.department:
            profile.department = payload.department
            profile.save()
    elif target_role == CustomUser.Role.AGENT:
        profile = getattr(created_user, 'agent_profile', None)

    return 201, {
        "status": "success",
        "message": f"Account with role '{created_user.role}' successfully provisioned.",
        "user_id": created_user.id,
        "profile_id": str(profile.id) if profile else None
    }

@router.get("/staff", response={200: StaffListResponseOut, 403: dict})
def all_staff(request):
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access denied. Administrator credentials required to view staff records."}

    staff_queryset = (
        CustomUser.objects.filter(role=CustomUser.Role.STAFF)
        .select_related('staff_profile', 'base_location__state')
        .order_by('-date_joined')
    )
    results = []
    for u in staff_queryset:
        loc_str = f"{u.base_location.name}" if u.base_location else ""
        if u.base_location and hasattr(u.base_location, 'state') and u.base_location.state:
            loc_str = f"{u.base_location.name}, {u.base_location.state.name}"
        
        results.append({
            "id": u.id,
            "email": u.email,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "phone_number": getattr(u, 'phone_number', ''),
            "base_location": loc_str,
            "street_address": getattr(u, 'street_address', ''),
            "department": getattr(u.staff_profile, 'department', '') if hasattr(u, 'staff_profile') else '',
            "role": u.role,
            "date_joined": u.date_joined,
        })

    return 200, {
        "staff_members": results
    }


@router.get("/agents", response={200: AgentListResponseOut, 403: dict})
def all_agents(request):
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access denied. Administrator credentials required to view agent records."}

    agent_queryset = (
        CustomUser.objects.filter(role=CustomUser.Role.AGENT)
        .select_related('agent_profile', 'base_location__state')
        .order_by('-date_joined')
    )

    agent_members = []
    for u in agent_queryset:
        loc_str = f"{u.base_location.name}" if u.base_location else ""
        if u.base_location and hasattr(u.base_location, 'state') and u.base_location.state:
            loc_str = f"{u.base_location.name}, {u.base_location.state.name}"

        agent_members.append({
            "id": u.id,
            "email": u.email,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "phone_number": getattr(u, 'phone_number', ''),
            "base_location": loc_str,
            "street_address": getattr(u, 'street_address', ''),
            "role": u.role,
            "date_joined": u.date_joined,
        })

    return 200, {
        "agent_members": agent_members
    }

@router.post("/staff/{staff_id}/toggle-suspension", response={200: dict, 404: dict, 403: dict})
@log_system_action(
    action_name=lambda res, req, staff_id, **kw: "ACTIVATE_STAFF" if "active" in res[1].get("message", "") else "SUSPEND_STAFF",
    target_getter=lambda req, staff_id, **kw: f"Staff Member #{staff_id}"
)
def toggle_staff_suspension(request, staff_id: str):
    """
    Toggles the active suspension state of a staff member's underlying user account.
    """
    if not _is_admin(request.user):
        return 403, {"detail": "Admin privileges required."}

    # Resolve the target: a StaffProfile id first, else a user id restricted to staff/agent roles.
    try:
        user = StaffProfile.objects.select_related('user').get(id=staff_id).user
    except (StaffProfile.DoesNotExist, ValueError):
        user = None
        if str(staff_id).isdigit():
            user = User.objects.filter(
                id=int(staff_id), role__in=[CustomUser.Role.STAFF, CustomUser.Role.AGENT]
            ).first()
        if user is None:
            return 404, {"detail": "Staff member profile not found."}

    # The old check let any is_staff account suspend ANY user, including administrators.
    if user.is_superuser or user.role == CustomUser.Role.ADMIN:
        return 403, {"detail": "Administrator accounts cannot be suspended from here."}
    if user.pk == request.user.pk:
        raise HttpError(400, "You cannot suspend your own account.")

    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])

    status_label = "activated" if user.is_active else "suspended"
    return 200, {
        "status": "success",
        "message": f"Staff account successfully {status_label}.",
        "is_active": user.is_active
    }

# ###########################################################
# Subscription Plan Management APIs
# ###########################################################

@router.get("/subscription-plans", response={200: list[SubscriptionPlanOutSchema], 403: dict})
def list_subscription_plans(request):
    """
    API endpoint for platform administrators and merchants to list all available subscription plans.
    """
    user = request.auth or getattr(request, 'user', None)

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access denied. Administrator credentials required to view subscription plans."}

    plans = SubscriptionPlan.objects.all().order_by('price')
    return 200, plans
    
@router.post("/subscription-plans", response={201: SubscriptionPlanOutSchema, 403: dict, 400: dict})
def admin_create_subscription_plan(request, payload: SubscriptionPlanCreateSchema):
    """
    API endpoint for platform administrators to securely compile, validate,
    and provision new core subscription tiers.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Administrator credentials required."}

    try:
        plan = SubscriptionPlan.objects.create(
            name=payload.name,
            price=Decimal(str(payload.price)),
            description=payload.description,
            features_raw=payload.features_raw,
            max_monthly_orders=payload.max_monthly_orders,
            max_products=payload.max_products,
            has_priority_dispatch=payload.has_priority_dispatch,
            has_featured_placement=payload.has_featured_placement,
            has_advanced_analytics=payload.has_advanced_analytics,
            is_available=payload.is_available if payload.is_available is not None else True
        )
        return 201, plan
    except Exception as e:
        return 400, {"detail": f"Failed to provision subscription tier: {str(e)}"}

@router.put("/subscription-plans/{plan_id}", response={200: SubscriptionPlanOutSchema, 403: dict, 404: dict, 400: dict})
def admin_update_subscription_plan(request, plan_id: int, payload: SubscriptionPlanUpdateSchema):
    """
    API endpoint for platform administrators to modify existing subscription tier parameters.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Administrator credentials required."}

    plan = get_object_or_404(SubscriptionPlan, id=plan_id)

    try:
        for attr, value in payload.dict(exclude_unset=True).items():
            if attr == 'price' and value is not None:
                setattr(plan, attr, Decimal(str(value)))
            else:
                setattr(plan, attr, value)
        
        if payload.name:
            plan.slug = '' 

        plan.save()
        return 200, plan
    except Exception as e:
        return 400, {"detail": f"Failed to update subscription tier: {str(e)}"}

@router.delete("/subscription-plans/{plan_id}", response={200: dict, 403: dict, 404: dict, 400: dict})
def admin_delete_subscription_plan(request, plan_id: int):
    """
    API endpoint for platform administrators to delete a subscription tier.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Administrator credentials required."}

    plan = get_object_or_404(SubscriptionPlan, id=plan_id)

    if plan.vendorsubscription_set.filter(is_active=True).exists():
        return 400, {"detail": "Cannot delete subscription plan while active vendors are subscribed to it. Deactivate the plan instead."}

    try:
        plan.delete()
        return 200, {"status": "success", "message": f"Subscription plan '{plan.name}' deleted successfully."}
    except Exception as e:
        return 400, {"detail": f"Failed to delete subscription plan: {str(e)}"}
    
@router.get("/subscriptions/vendors", response=List[VendorSubscriptionDetailOut])
def list_vendor_subscriptions_audit(request):
    """Audit all vendor subscription statuses across the platform."""
    _require_admin(request.auth)

    subs = VendorSubscription.objects.select_related('vendor_profile__user', 'plan').all()
    
    result = []
    for sub in subs:
        vendor_user = sub.vendor_profile.user
        result.append({
            "id": sub.id,
            "vendor_name": sub.vendor_profile.company_name,
            "vendor_email": vendor_user.email,
            "plan_name": sub.plan.name if sub.plan else None,
            "plan_price": float(sub.plan.price) if sub.plan else None,
            "is_active": sub.is_active,
            "has_valid_access": sub.has_valid_access,
            "subscription_expires_at": sub.subscription_expires_at,
        })
    return result

# ###########################################################
# Customer & Rider Management APIs
# ###########################################################

@router.get("/customers", response={200: list, 403: dict})
def list_admin_customers(request):
    """
    Retrieves all registered customer profiles directly from IndividualProfile.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Admin privileges required."}

    users_qs = IndividualProfile.objects.select_related('user').all()
    customers_data = []

    for profile in users_qs:
        user_obj = profile.user
        
        full_name = f"{profile.first_name} {profile.last_name}".strip()
        delivery_address = profile.default_delivery_address or 'No Address Set'

        customers_data.append({
            "id": profile.id,
            "email": getattr(user_obj, 'email', 'N/A'),
            "full_name": full_name,
            "phone_number": getattr(user_obj, 'phone_number', 'N/A'),
            "default_delivery_address": delivery_address,
            "is_active": getattr(user_obj, 'is_active', True),
            "date_joined": user_obj.date_joined.strftime("%Y-%m-%d %H:%M") if user_obj and hasattr(user_obj, 'date_joined') and user_obj.date_joined else 'N/A'
        })

    return 200, customers_data

# ###########################################################
# Rider Management APIs
# ###########################################################
@router.get("/riders", response={200: list, 403: dict})
def list_admin_riders(request):
    """
    Retrieves all registered rider profiles for admin management.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        raise HttpError(403, "Admin privileges required.")
    
    riders_qs = RiderProfile.objects.select_related('user').all()
    riders_data = []
    for profile in riders_qs:
        user_obj = profile.user
        
        full_name = f"{profile.first_name} {profile.last_name}".strip() if hasattr(profile, 'first_name') else getattr(user_obj, 'username', 'Rider')
        delivery_address = getattr(profile, 'default_delivery_address', None) or getattr(profile, 'base_location', 'No Location Set')

        riders_data.append({
            "id": profile.id,
            "email": getattr(user_obj, 'email', 'N/A'),
            "full_name": full_name,
            "phone_number": getattr(user_obj, 'phone_number', 'N/A'),
            "default_delivery_address": delivery_address,
            "is_active": getattr(user_obj, 'is_active', True),
            "date_joined": user_obj.date_joined.strftime("%Y-%m-%d %H:%M") if user_obj and hasattr(user_obj, 'date_joined') and user_obj.date_joined else 'N/A'
        })

    return 200, riders_data

@router.get("/riders/{rider_id}/detail", response={200: dict, 403: dict, 404: dict})
def get_admin_rider_detail(request, rider_id: str):
    """
    Retrieves a specific rider profile along with their complete review history.
    """
    if not request.user or not request.user.is_authenticated or not request.user.is_staff:
        return 403, {"detail": "Admin privileges required."}

    try:
        rider = RiderProfile.objects.select_related('user').get(id=rider_id)
    except RiderProfile.DoesNotExist:
        return 404, {"detail": "Rider profile not found."}

    user_obj = rider.user
    reviews_qs = RiderReview.objects.filter(rider=rider).select_related('customer').order_by('-created_at')
    
    reviews_list = []
    total_rating = 0
    review_count = 0

    for review in reviews_qs:
        total_rating += review.rating
        review_count += 1
        
        customer_name = "Anonymous Customer"
        if hasattr(review, 'customer') and review.customer:
            customer_name = getattr(review.customer, 'full_name', 'Customer')

        reviews_list.append({
            "id": review.id,
            "rating": review.rating,
            "comment": getattr(review, 'comment', ''),
            "customer_name": customer_name,
            "created_at": review.created_at.strftime("%Y-%m-%d %H:%M") if review.created_at else 'N/A'
        })

    average_rating = round(total_rating / review_count, 1) if review_count > 0 else 0.0

    full_name = f"{rider.first_name} {rider.last_name}".strip() if hasattr(rider, 'first_name') else getattr(user_obj, 'username', 'Rider')
    delivery_address = getattr(rider, 'default_delivery_address', None) or getattr(rider, 'base_location', 'No Location Set')

    rider_data = {
        "id": rider.id,
        "email": getattr(user_obj, 'email', 'N/A'),
        "full_name": full_name,
        "phone_number": getattr(user_obj, 'phone_number', 'N/A'),
        "bike_plate_number": getattr(rider, 'bike_plate_number', 'N/A'),
        "device_type": getattr(rider, 'device_type', 'smartphone'),
        "base_location": getattr(rider, 'base_location', 'N/A'),
        "street_address": getattr(rider, 'street_address', 'N/A'),
        "default_delivery_address": delivery_address,
        "id_type": getattr(rider, 'id_type', 'N/A'),
        "id_number": getattr(rider, 'id_number', 'N/A'),
        "guarantor_name": getattr(rider, 'guarantor_name', 'N/A'),
        "guarantor_phone": getattr(rider, 'guarantor_phone', 'N/A'),
        "guarantor_address": getattr(rider, 'guarantor_address', 'N/A'),
        "is_vetted": getattr(rider, 'is_vetted', False),
        "is_id_verified": getattr(rider, 'is_id_verified', False),
        "is_active": getattr(user_obj, 'is_active', True),
        "date_joined": user_obj.date_joined.strftime("%Y-%m-%d %H:%M") if user_obj and hasattr(user_obj, 'date_joined') and user_obj.date_joined else 'N/A',
        "performance": {
            "total_reviews": review_count,
            "average_rating": average_rating
        },
        "reviews": reviews_list
    }

    return 200, rider_data

@router.post("/riders/{rider_id}/verify-id", response={200: dict, 403: dict, 404: dict})
@log_system_action(
    action_name=lambda res, req, rider_id, approved, **kw: "VERIFY_RIDER_ID" if approved else "REJECT_RIDER_ID",
    target_getter=lambda req, rider_id, **kw: f"Rider Profile #{rider_id}"
)
def verify_rider_id(request, rider_id: str, approved: bool):
    if not request.user or not request.user.is_staff:
        raise HttpError(403, "Admin privileges required.")
    
    rider = get_object_or_404(RiderProfile, id=rider_id)
    rider.is_id_verified = approved
    rider.is_vetted = approved
    rider.is_active = approved
    rider.save()

    status_text = "verified and activated" if approved else "rejected"
    return 200, {
        "status": "success",
        "message": f"Rider ID successfully {status_text}."
    }

@router.post("/riders/{rider_id}/toggle-suspension", response={200: dict, 404: dict, 403: dict})
@log_system_action(
    action_name=lambda res, req, rider_id, **kw: "ACTIVATE_rider" if "active" in res[1].get("message", "") else "SUSPEND_RIDER",
    target_getter=lambda req, rider_id, **kw: f"Rider Profile #{rider_id}"
)
def toggle_rider_suspension(request, rider_id: str):
    """
    Toggles the active suspension state of a rider's underlying user account.
    """
    if not request.user or not request.user.is_authenticated or not request.user.is_staff:
        return 403, {"detail": "Admin privileges required."}

    try:
        profile = RiderProfile.objects.select_related('user').get(id=rider_id)
    except RiderProfile.DoesNotExist:
        return 404, {"detail": "Rider profile not found."}

    user_obj = profile.user
    user_obj.is_active = not user_obj.is_active
    user_obj.save()

    status_label = "activated" if user_obj.is_active else "suspended"
    return 200, {
        "status": "success",
        "message": f"Rider account successfully {status_label}.",
        "is_active": user_obj.is_active
    }

@router.get("/financials/rider-payouts", response=List[RiderPayoutAuditOut])
def audit_rider_payouts(request, schedule: Optional[str] = None):
    """
    Audit riders by payout schedule (daily/weekly) and view wallet balances.
    """
    if request.user.role not in ['admin', 'staff']:
        raise HttpError(403, "Access restricted.")
    
    queryset = RiderProfile.objects.select_related('user').all()
    if schedule:
        queryset = queryset.filter(payout_schedule=schedule)

    results = []
    for rider in queryset:
        wallet_balance = float(getattr(rider, 'wallet_balance', 0.0))
        total_deliveries = getattr(rider, 'total_deliveries', 0)

        results.append({
            "id": str(rider.id),
            "rider_name": f"{rider.first_name} {rider.last_name}",
            "phone": getattr(rider.user, 'phone_number', None) or getattr(rider, 'phone_number', 'N/A'),
            "bike_plate_number": rider.bike_plate_number,
            "payout_schedule": rider.payout_schedule,
            "payout_method": rider.payout_method,
            "wallet_balance": wallet_balance,
            "total_deliveries": total_deliveries,
            "is_active": rider.is_active
        })
    
    return results

@router.post("/financials/rider-payouts/{rider_id}/mark-paid")
def mark_rider_payout_completed(request, rider_id: str):
    """
    Marks a rider's accumulated balance as settled/paid out.
    """
    if request.user.role not in ['admin', 'staff']:
        raise HttpError(403, "Access restricted.")
    
    try:
        rider = RiderProfile.objects.get(id=rider_id)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    return {"status": "success", "message": f"Payout successfully marked as paid for rider {rider.first_name}."}


# ###########################################################
# Order Management APIs
# ###########################################################

@router.post("/order/{order_id}/reassign")
def admin_reassign_order(request, order_id: str, data: ReassignOrderSchema):
    """
    Reassigns an active order to a new delivery rider.
    """
    user = request.auth or request.user
    if not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")

    if not (user.is_superuser or getattr(user, 'role', None) == 'admin'):
        raise HttpError(403, "Access Denied: Administrator privileges required.")

    def execute_admin_reassignment():
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update(of=('self',)).select_related('sender').get(id=order_id)
            except (Order.DoesNotExist, ValueError):
                raise HttpError(404, "Order trace not found.")

            # 'dispatched' is not a valid order status; ASSIGNED is. Only acknowledged or
            # already-assigned orders can be (re)assigned: never delivered/cancelled ones.
            if order.status not in (Order.OrderStatus.ACKNOWLEDGED, Order.OrderStatus.ASSIGNED):
                raise HttpError(400, "Only acknowledged or assigned orders can be reassigned to a rider.")

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

    rider_name = execute_admin_reassignment()
    return {
        "status": "success", 
        "message": f"Order #{order_id} successfully reassigned to {rider_name}."
    }

# ###########################################################
# Audit Logs & Dispute Management APIs
# ###########################################################
@router.get("/audits/logs", response=List[AuditLogOut])
def list_audit_logs(request):
    if request.user.role != 'admin':
        raise HttpError(403, "Access restricted.")
    
    logs = SystemAuditLog.objects.select_related('user').all().order_by('-created_at')[:100]
    return [
        {
            "id": log.id,
            "user_email": log.user.email if log.user else "System Automation",
            "action": log.action,
            "target_entity": log.target_entity,
            "details": log.details,
            "created_at": log.created_at,
        } for log in logs
    ]

@router.get("/disputes", response=List[DisputeOut])
def list_disputes(request):
    if request.user.role not in ['admin', 'staff']:
        raise HttpError(403, "Access restricted.")
    
    disputes = OrderDisputeRefund.objects.all().order_by('-created_at')
    return [
        {
            "id": d.id,
            "order_id": d.order_id,
            "customer_name": d.customer_name,
            "reason": d.reason,
            "amount": float(d.amount),
            "status": d.status,
            "raised_by": d.raised_by,
            "created_at": d.created_at,
        } for d in disputes
    ]

@router.post("/disputes/{dispute_id}/resolve")
@log_system_action(
    action_name=lambda res, req, dispute_id, action, **kw: f"REFUND_{action.upper()}" if action in ['approve', 'reject'] else "RESOLVE_DISPUTE",
    target_getter=lambda req, dispute_id, action, **kw: f"Dispute #{dispute_id}"
)
def resolve_dispute(request, dispute_id: int, action: str):
    if request.user.role != 'admin':
        raise HttpError(403, "Access restricted.")
    
    try:
        dispute = OrderDisputeRefund.objects.get(id=dispute_id)
        dispute.status = 'approved' if action == 'approve' else 'rejected'
        dispute.resolved_by = request.user
        dispute.save()
    except OrderDisputeRefund.DoesNotExist:
        raise HttpError(404, "Dispute record not found.")

    return {"status": "success", "message": f"Dispute successfully {dispute.status}."}

# ##############################################################################
# CUSTOMER REVIEW MANAGEMENT
# ###############################################################################
@router.get("/reviews")
def admin_list_customer_reviews(request):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")

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
@log_system_action(
    action_name=lambda res, req, review_id, **kw: "APPROVE_REVIEW",
    target_getter=lambda req, review_id, **kw: f"Customer Review #{review_id}"
)
def admin_approve_review(request, review_id: int):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")

    review = get_object_or_404(CustomerReview, id=review_id)
    review.is_approved = True
    review.save(update_fields=['is_approved'])
    return {"status": "success", "message": "Review approved successfully."}

@router.delete("/reviews/{review_id}/delete")
@log_system_action(
    action_name=lambda res, req, review_id, **kw: "DELETE_REVIEW",
    target_getter=lambda req, review_id, **kw: f"Delete Customer Review #{review_id}"
)
def admin_delete_review(request, review_id: int):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")

    review = get_object_or_404(CustomerReview, id=review_id)
    review.delete()
    return {"status": "success", "message": "Review deleted successfully."}

@router.get("/faqs")
def admin_list_faqs(request):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")
    
    faqs = FrequentlyAskedQuestion.objects.all()
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
@log_system_action(
    action_name=lambda res, req, **kw: "CREATE_FAQs",
    target_getter=lambda req, **kw: f"FAQs Created"
)
def admin_create_faq(request, payload: FAQSchema):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")

    faq = FrequentlyAskedQuestion.objects.create(
        question=payload.question,
        answer=payload.answer,
        order=payload.order,
        is_active=payload.is_active
    )
    return {"status": "success", "message": "FAQ created successfully.", "id": faq.id}

@router.delete("/faqs/{faq_id}/delete")
@log_system_action(
    action_name=lambda res, req, faq_id, **kw: "DELETE_FAQs",
    target_getter=lambda req, faq_id, **kw: f"Delete FAQs #{faq_id}"
)
def admin_delete_faq(request, faq_id: int):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")

    faq = get_object_or_404(FrequentlyAskedQuestion, id=faq_id)
    faq.delete()
    return {"status": "success", "message": "FAQ deleted successfully."}


@router.get("/newsletter/subscribers")
def admin_list_subscribers(request):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")
    
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
@log_system_action(
    action_name=lambda res, req, **kw: "BROADCAST_NEWLETTER",
    target_getter=lambda req, **kw: f"Broadcast news letter"
)
def admin_broadcast_newsletter(request, payload: NewsletterBroadcastSchema):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated or not getattr(user, 'is_superuser', False):
        raise HttpError(403, "Admin access required.")

    try:
        sent_count, total_recipients = send_newsletter(payload.subject, payload.content, payload.template_type)
    except ValueError as e:
        raise HttpError(400, str(e))
    if sent_count == 0:
        raise HttpError(500, "Failed to send the email broadcast. Please try again.")

    NewsletterBroadcast.objects.create(
        subject=payload.subject,
        content=payload.content,
        template_type=payload.template_type,
        sent_by=user,
        recipient_count=sent_count
    )

    return {"status": "success", "message": f"Newsletter broadcasted using '{payload.template_type}' template to {sent_count} of {total_recipients} subscribers."}

@router.post("/broadcasts/send", response={200: dict}, auth=JWTAuth())
def send_system_broadcast(request, payload: BroadcastCreateSchema):
    user = request.auth or getattr(request, 'user', None)
    if not user or not user.is_authenticated or (not user.is_superuser and not user.is_staff):
        raise HttpError(403, "Management authorization required.")

    if payload.target_audience not in ('all', 'vendors', 'merchants', 'riders', 'customers'):
        raise HttpError(400, "Invalid target audience.")

    broadcast = SystemBroadcast.objects.create(
        title=payload.title,
        message=payload.message,
        target_audience=payload.target_audience,
        sent_by=user
    )

    broadcast_data = {
        "title": broadcast.title,
        "text": broadcast.message,
        "audience": broadcast.target_audience,
        "created_at": broadcast.created_at.isoformat()
    }

    push_payload = {
        "head": broadcast.title,
        "body": broadcast.message,
        "icon": "/images/logo.png",
        "url": "/"
    }

    group_mapping = {
        'all': 'all_users',
        'merchants': 'merchants',
        'vendors': 'merchants',
        'riders': 'riders',
        'customers': 'customers'
    }
    # WebSocket groups the dashboards actually join (see the consumers).
    ws_group_mapping = {
        'all_users': 'platform_global_broadcasts',
        'merchants': 'vendor_broadcasts',
        'riders': 'rider_broadcasts',
        'customers': 'customer_broadcasts',
    }

    target_group_name = group_mapping.get(payload.target_audience, 'all_users')

    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                ws_group_mapping[target_group_name],
                {
                    "type": "system_broadcast_message",
                    "message": broadcast_data
                }
            )
    except Exception as ws_err:
        print(f"WebSocket broadcast error: {ws_err}")

    try:
        WebPushGroup.objects.get_or_create(name=target_group_name)
        send_group_notification(group_name=target_group_name, payload=push_payload)
    except Exception as push_err:
        print(f"Web push dispatch warning: {push_err}")

    return {"status": "success", "message": f"Broadcast '{broadcast.title}' processed and dispatched successfully!"}

@router.post("/locations", response={201: LocationResponse})
def create_coverage_area(request, payload: LocationCreateInput):
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        raise HttpError(403, "Admin privileges required.")

    state_title = payload.state_name.strip().title()
    area_title = payload.area_name.strip().title()

    try:
        with transaction.atomic():
            state_obj, _ = State.objects.get_or_create(
                name__iexact=state_title,
                defaults={'name': state_title}
            )
            
            area = CoverageArea(
                state=state_obj,
                name=area_title,
                latitude=payload.latitude,
                longitude=payload.longitude
            )
            area.save() 
    except IntegrityError:
        raise HttpError(400, f"The location '{area_title}' in '{state_obj.name}' already exists.")
    
    return 201, {
        "id": area.id,
        "name": area.name,
        "state_name": area.state.name
    }

@router.get("/platform/referral", response={200: PlatformSettingOutSchema, 403: dict, 500: dict})
def admin_get_platform_referral_setting(request):
    """
    API endpoint for platform administrators to fetch the current singleton core platform referral settings.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Administrator credentials required."}

    try:
        settings_obj = PlatformSetting.get_settings()
        return 200, settings_obj
    except Exception as e:
        return 500, {"detail": f"Failed to retrieve platform referral settings: {str(e)}"}
    
@router.post("/platform/referral", response={200: PlatformSettingOutSchema, 403: dict, 400: dict})
def admin_create_platform_referral_setting_plan(request, payload: PlatformSettingCreateSchema):
    """
    API endpoint for platform administrators to securely update or provision the singleton core platform referral settings.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated or (user.role != CustomUser.Role.ADMIN and not user.is_superuser):
        return 403, {"detail": "Access unauthorized. Administrator credentials required."}

    try:
        plan, created = PlatformSetting.objects.update_or_create(
            pk=1,
            defaults={
                'referral_order_threshold': payload.referral_order_threshold,
                'referral_bonus_amount': payload.referral_bonus_amount
            }
        )
        
        status_code = 201 if created else 200
        return status_code, plan
    except Exception as e:
        return 400, {"detail": f"Failed to update platform referral settings: {str(e)}"}

@router.get("/delivery/config", response=DeliveryConfigOut)
def get_delivery_config(request):
    """Fetch current global delivery pricing configuration."""
    # Enforce admin permission check here based on your auth setup
    # e.g., if not request.user.is_staff: raise HttpError(403, "Admin access required.")
    
    config = DeliveryConfig.get_solo()
    return {
        "full_time_rider_earning": config.full_time_rider_earning,
        "casual_platform_percentage": config.casual_platform_percentage,
        "updated_at": config.updated_at.isoformat()
    }

@router.put("/delivery/config", response=DeliveryConfigOut)
def update_delivery_config(request, payload: DeliveryConfigUpdateIn):
    """Update global delivery earnings and platform cut percentages."""
    # if not request.user.is_staff: raise HttpError(403, "Admin access required.")
    
    config = DeliveryConfig.get_solo()

    # Update fields only if provided in the payload
    if payload.full_time_rider_earning is not None:
        config.full_time_rider_earning = payload.full_time_rider_earning
    
    if payload.casual_platform_percentage is not None:
        config.casual_platform_percentage = payload.casual_platform_percentage

    config.save()

    return {
        "full_time_rider_earning": config.full_time_rider_earning,
        "casual_platform_percentage": config.casual_platform_percentage,
        "updated_at": config.updated_at.isoformat()
    }