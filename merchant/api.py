# marketplace/merchant_api.py
import json
import os
import logging
from decimal import Decimal
from typing import List, Optional
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.db.models import Sum, Count, Q, OuterRef, Exists, Prefetch
from django.db.models.functions import TruncDate
from django.db import transaction, IntegrityError
from django.core.files.base import ContentFile
from django.utils.text import slugify
from asgiref.sync import sync_to_async
from ninja import Router, Form, File, Schema, Field
from ninja.errors import HttpError
from ninja.files import UploadedFile
from datetime import datetime
from django.utils import timezone
from datetime import timedelta
# Authenticator and Schema imports
from accounts.models import CoverageArea
from accounts.utils import JWTAuth, generate_alphanumeric_id
from .schemas import (BusinessProfileResponse, UpdateBusinessProfileInput, 
    ProductDetailPayloadSchema, ProductDetailPayloadSchema,
    ActiveOrdersListSchema, CoordinateUpdatePayload, ShippingPreferenceInput,
    MerchantDashboardTransactionItemSchema, MerchantDashboardTransactionsOutSchema
)
from wallet.models import VendorWallet
from wallet.schemas import SetTransferPinSchema
# Model imports
from order.models import Order as LogisticsOrder
from merchant.models import MerchantProfile, StorefrontConfig, VendorShippingPreference
from marketplace.schemas import (
    ProductSavePayloadSchema, BatchSKUOverrideUpdateSchema,
    InitialDropdownsResponseSchema, ProductAddSuccessResponse
)
from marketplace.models import (
    MarketplaceOrder, Product, VariantOption, VariantValue, 
    ProductItem, ProductImage, SubCategory, Brand, MarketplaceOrderItem,
    MenuItemOption
    
)
from payment.models import VendorSubscription
from marketing.models import SystemBroadcast
from .utils import active_merchant_access_q, evaluate_merchant_subscription_or_trial




router = Router(tags=["Merchant Dashboard & Inventory Management"], auth=JWTAuth())

# Initialize a standard production logger
logger = logging.getLogger("aika.merchant")

TRIAL_MAX_PRODUCTS = 15

def parse_json_field(value, field_name):
    if not isinstance(value, str) or not value.strip():
        return value

    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON supplied for '{field_name}'.") from exc

# =========================================================================
# 1. MERCHANT ANALYTICS DASHBOARD SUMMARY
# =========================================================================

async def _fetch_merchant_dashboard_transactions(vendor_profile, limit: int = 20) -> tuple[list[dict], int]:
    paid_statuses = {
        "paid",
        "processing",
        "out_for_delivery",
        "delivered",
        "received",
        "completed",
        "settled",
        "released",
    }
    received_statuses = {"received", "completed", "settled", "released"}

    orders_qs = (
        MarketplaceOrder.objects.filter(vendor=vendor_profile)
        .select_related("customer__user")
        .order_by("-created_at")[:limit]
    )

    tx_list: list[dict] = []
    pending_count = 0

    async for order in orders_qs:
        status_val = str(getattr(order, "status", "") or "").strip().lower()
        is_paid = bool(
            getattr(order, "is_paid", False)
            or getattr(order, "paid_at", None)
            or status_val in paid_statuses
        )
        if not is_paid:
            continue

        is_received = bool(
            status_val in received_statuses
            or getattr(order, "is_received", False)
            or getattr(order, "customer_confirmed_receipt", False)
            or getattr(order, "received_at", None)
            or getattr(order, "customer_confirmed_at", None)
        )
        if not is_received:
            pending_count += 1

        raw_amount = (
            getattr(order, "vendor_payout", None)
            or getattr(order, "subtotal", None)
            or getattr(order, "total_price", 0)
        )
        amount = float(raw_amount or 0)

        customer_user = getattr(getattr(order, "customer", None), "user", None)
        customer_name = (
            f"{getattr(customer_user, 'first_name', '')} {getattr(customer_user, 'last_name', '')}".strip()
            or getattr(customer_user, "email", None)
            or "Customer"
        )

        reference = (
            getattr(order, "payment_reference", None)
            or getattr(order, "reference", None)
            or f"ORD-{order.id}"
        )

        tx_list.append({
            "id": f"order-{order.id}",
            "order_id": str(order.id),
            "reference": str(reference),
            "title": f"Order #{order.id}",
            "customer_name": customer_name,
            "amount": amount,
            "status": "completed" if is_received else "pending",
            "balance_bucket": "available" if is_received else "escrow",
            "status_label": "Available" if is_received else "Pending (In Escrow)",
            "created_at": getattr(order, "created_at", None),
        })

    return tx_list, pending_count

@router.get("/dashboard/summary")
def get_merchant_dashboard_summary_api(request):
    """
    Synchronously aggregates marketplace order metrics, history logs,
    and dynamic chart trends for the active merchant profile.
    """
    try:
        profile = MerchantProfile.objects.select_related("user").get(user=request.user)
    except MerchantProfile.DoesNotExist:
        return JsonResponse({"detail": "Merchant identity profile not found."}, status=403)

    # --- Fetch Subscription & 7-Day Trial Status ---
    access_status = evaluate_merchant_subscription_or_trial(profile)

    try:
        subscription = VendorSubscription.objects.select_related("plan").get(vendor_profile=profile)
        sub_has_access = bool(subscription.has_valid_access)
        sub_plan_name = subscription.plan.name if subscription.plan else None
        remaining_orders = subscription.remaining_orders
    except VendorSubscription.DoesNotExist:
        sub_has_access = False
        sub_plan_name = None
        remaining_orders = None

    has_valid_access = bool(sub_has_access or access_status.has_access)

    if sub_has_access and sub_plan_name:
        plan_name = sub_plan_name
    elif access_status.is_on_trial:
        plan_name = f"7-Day Free Trial ({access_status.trial_days_remaining}d left)"
    elif access_status.reason == "trial_expired":
        plan_name = "Free Trial Expired"
    else:
        plan_name = sub_plan_name or "No Active Plan"

    metrics_query = MarketplaceOrder.objects.filter(vendor=profile).aggregate(
        total_rev=Sum("subtotal", filter=Q(status="completed")),
        total_count=Count("id"),
        completed_count=Count("id", filter=Q(status="completed")),
        active_count=Count("id", filter=Q(status="processing")),
    )

    total_revenue = float(metrics_query["total_rev"] or 0.0)
    orders_count = metrics_query["total_count"] or 0
    completed_count = metrics_query["completed_count"] or 0
    active_deliveries = metrics_query["active_count"] or 0

    success_rate = round((completed_count / orders_count) * 100, 1) if orders_count > 0 else 0.0

    # =========================================================================
    # DYNAMIC CHART TRENDS AGGREGATION (Past 7 Days)
    # =========================================================================
    today = timezone.now().date()
    start_date = today - timedelta(days=6)

    recent_trend_qs = (
        MarketplaceOrder.objects.filter(
            vendor=profile,
            created_at__date__gte=start_date,
        )
        .annotate(order_date=TruncDate("created_at"))
        .values("order_date")
        .annotate(
            daily_revenue=Sum("subtotal"),
            daily_count=Count("id"),
        )
        .order_by("order_date")
    )

    trend_map = {}
    for entry in recent_trend_qs:
        d_str = str(entry["order_date"])
        trend_map[d_str] = {
            "revenue": float(entry["daily_revenue"] or 0.0),
            "count": int(entry["daily_count"] or 0),
        }

    days_labels = []
    days_revenue = []
    days_volumes = []

    for i in range(7):
        current_d = start_date + timedelta(days=i)
        d_str = str(current_d)
        days_labels.append(current_d.strftime("%a"))

        data_point = trend_map.get(d_str, {"revenue": 0.0, "count": 0})
        days_revenue.append(data_point["revenue"])
        days_volumes.append(data_point["count"])

    # =========================================================================
    # RECENT ORDERS LIST
    # =========================================================================
    recent_orders = []
    orders_queryset = (
        MarketplaceOrder.objects.filter(vendor=profile)
        .select_related("customer", "customer__user", "logistics_dispatch")
        .order_by("-created_at")[:10]
    )

    for order in orders_queryset:
        c_name = order.customer.first_name if order.customer else "Walk-in Customer"
        c_phone = order.customer.user.phone_number if (order.customer and order.customer.user) else ""

        ref = getattr(order.logistics_dispatch, "order_reference", order.id) or order.id

        recent_orders.append({
            "id": order.id,
            "order_reference": ref,
            "customer_name": c_name,
            "customer_phone": c_phone,
            "total_amount": float(order.subtotal),
            "status": str(order.status).lower().strip(),
        })

    return {
        "status": "success",
        "message": "Ledger logs aggregated successfully.",
        "data": {
            "subscription": {
                "has_valid_access": has_valid_access,
                "plan_name": plan_name,
                "remaining_orders": remaining_orders,
                "is_on_trial": access_status.is_on_trial,
                "trial_days_remaining": access_status.trial_days_remaining,
                "trial_ends_at": profile.trial_ends_at.isoformat() if profile.trial_ends_at else None,
                "access_reason": access_status.reason,
            },
            "metrics": {
                "total_revenue": total_revenue,
                "orders_count": orders_count,
                "success_rate": success_rate,
                "active_deliveries": active_deliveries,
            },
            "recent_orders": recent_orders,
            "chart_trends": {
                "days_labels": days_labels,
                "days_revenue": days_revenue,
                "days_volumes": days_volumes,
            },
        },
    }


@router.get("/dashboard/transactions", response=MerchantDashboardTransactionsOutSchema, auth=JWTAuth())
async def get_merchant_dashboard_transactions(request, limit: int = 20):
    """
    Returns recent paid order transactions for the main merchant dashboard,
    showing orders awaiting buyer receipt confirmation as 'pending' (in escrow).
    """
    user = request.auth
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication credentials required.")

    vendor = await MerchantProfile.objects.filter(user=user).afirst()
    if not vendor:
        raise HttpError(404, "Merchant profile not found.")

    wallet = await VendorWallet.objects.filter(profile=vendor).afirst()
    safe_limit = max(1, min(limit, 100))
    transactions, pending_count = await _fetch_merchant_dashboard_transactions(vendor, limit=safe_limit)

    return {
        "status": "success",
        "escrow_balance": float(wallet.escrow_balance) if wallet else 0.0,
        "available_balance": float(wallet.available_balance) if wallet else 0.0,
        "pending_count": pending_count,
        "transactions": transactions,
    }

# =========================================================================
# 2. INVENTORY LIST VIEW
# =========================================================================
@router.get("/inventory")
def list_merchant_inventory_api(request):
    """
    Asynchronously queries all catalog profiles owned by the logged-in merchant,
    annotating real-time structural variant presence parameters.
    """
    user = request.user
    if user.role != 'business':
         return JsonResponse({"detail": "Access Denied: Account channel role holds insufficient merchant parameters."}, status=403)

    try:
        profile = MerchantProfile.objects.get(user=user)
    except MerchantProfile.DoesNotExist:
         return JsonResponse({"detail": "Merchant configuration details could not be localized."}, status=404)

    has_variants_query = VariantOption.objects.filter(product=OuterRef('pk'))
    
    products_queryset = Product.objects.filter(vendor=profile)\
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

        products_list.append({
            "id": product.id,
            "name": product.name,
            "price": float(product.price),
            "is_available": product.is_available,
            "category": category_label,
            "bonanza_mode": product.bonanza_mode,
            "discount_price": float(product.discount_price) if product.discount_price else None,
            "image": product.image.url if product.image else None,
            "has_variants": product.has_variants,
            "sku_items": sku_records
        })

    return {
        "status": "success",
        "message": f"Successfully loaded {len(products_list)} asset tracks for the merchant catalog.",
        "data": products_list
    }

# =========================================================================
# 3. ADD INVENTORY ITEM (MULTIPART/FORM DATA)
# =========================================================================

def _to_int_or_none(val):
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def _get_or_create_with_unique_slug(model_cls, lookup_kwargs, base_name, extra_defaults=None):
    clean_name = base_name.strip()
    base_slug = slugify(clean_name) or "item"
    defaults = {
        "slug": base_slug,
        **(extra_defaults or {}),
    }
    try:
        obj, _ = model_cls.objects.get_or_create(**lookup_kwargs, defaults=defaults)
        return obj
    except IntegrityError:
        for idx in range(1, 50):
            candidate_slug = f"{base_slug}-{idx}"
            try:
                obj, _ = model_cls.objects.get_or_create(
                    **lookup_kwargs,
                    defaults={**defaults, "slug": candidate_slug},
                )
                return obj
            except IntegrityError:
                continue
        raise

def _sync_add_product_worker(
    user,
    profile,
    store_type,
    data,
    key_features,
    variant_options,
    sku_items,
    gallery_images,
    request_files,
    menu_options=None,
):
    def safe_json_parse(val, default):
        if not val:
            return default
        if isinstance(val, (list, dict)):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return default
        return default

    key_features = safe_json_parse(key_features, [])
    variant_options = safe_json_parse(variant_options, [])
    sku_items = safe_json_parse(sku_items, [])
    gallery_images = safe_json_parse(gallery_images, [])
    menu_options = safe_json_parse(menu_options, [])

    def get_file_from_request(files_obj, key):
        if not files_obj:
            return None
        if hasattr(files_obj, "get"):
            return files_obj.get(key)
        if isinstance(files_obj, (list, tuple)):
            for f in files_obj:
                if getattr(f, "name", None) == key or str(f).endswith(key):
                    return f
            return files_obj[0] if files_obj else None
        return None

    with transaction.atomic():
        # 1. Derive base category from MerchantProfile
        if store_type == "restaurant" or getattr(profile, "business_type", "") == "restaurant":
            resolved_category = "restaurant"
        else:
            raw_cat = (
                getattr(profile, "ecommerce_category", None)
                or getattr(profile, "ecommerce_sub_category", None)
                or getattr(data, "category", None)
                or "general"
            )
            resolved_category = str(raw_cat).lower().strip() or "general"

        product = Product.objects.create(
            id=f"PRD-{generate_alphanumeric_id(8)}",
            vendor=profile,
            name=data.name.strip(),
            description=(data.description or "").strip(),
            key_features=key_features,
            price=data.price,
            category=resolved_category,
            bonanza_mode=bool(data.bonanza_mode),
            discount_price=data.discount_price,
            is_available=bool(data.is_available),
        )

        dirty_product = False

        primary_file = get_file_from_request(request_files, data.image_key or "image")
        if primary_file:
            product.image = primary_file
            dirty_product = True

        # 2. Subcategories (Restaurants & E-Commerce)
        sub_category_selection = str(data.sub_category_id or "").strip()
        if sub_category_selection == "CUSTOM" and (data.new_sub_category_name or "").strip():
            sub_cat = _get_or_create_with_unique_slug(
                SubCategory,
                lookup_kwargs={
                    "name__iexact": data.new_sub_category_name.strip(),
                    "primary_category": resolved_category,
                },
                base_name=data.new_sub_category_name,
                extra_defaults={"name": data.new_sub_category_name.strip()},
            )
            product.sub_category = sub_cat
            dirty_product = True
        elif sub_category_selection.isdigit():
            product.sub_category_id = int(sub_category_selection)
            dirty_product = True

        # 3. Brands (E-Commerce Only)
        if store_type != "restaurant":
            brand_selection = str(data.brand_selection or "NONE").strip()
            if brand_selection == "CUSTOM" and (data.new_brand_name or "").strip():
                brand = _get_or_create_with_unique_slug(
                    Brand,
                    lookup_kwargs={"name__iexact": data.new_brand_name.strip()},
                    base_name=data.new_brand_name,
                    extra_defaults={
                        "name": data.new_brand_name.strip(),
                        "created_by": user,
                    },
                )
                if product.sub_category:
                    brand.sub_categories.add(product.sub_category)
                product.brand = brand
                dirty_product = True
            elif brand_selection.isdigit():
                product.brand_id = int(brand_selection)
                dirty_product = True

        if dirty_product:
            product.save()

        # 4. Restaurant Menu Options
        if store_type == "restaurant" and menu_options:
            option_rows = []
            for opt_input in menu_options:
                if not isinstance(opt_input, dict):
                    continue
                opt_name = str(opt_input.get("name", "")).strip()
                opt_price = opt_input.get("price")
                if opt_name and opt_price is not None:
                    option_rows.append(
                        MenuItemOption(
                            product=product,
                            name=opt_name,
                            price=Decimal(str(opt_price)),
                            is_available=bool(opt_input.get("is_available", True)),
                        )
                    )
            if option_rows:
                MenuItemOption.objects.bulk_create(option_rows)

        # 5. Multi-Step Variant & SKU Synthesis (E-Commerce Only)
        elif store_type != "restaurant" and variant_options and sku_items:
            created_values_map = {}

            for opt_input in variant_options:
                if not isinstance(opt_input, dict):
                    continue
                opt_name = str(opt_input.get("name", "")).strip()
                if not opt_name:
                    continue
                option = VariantOption.objects.create(product=product, name=opt_name)

                raw_values = [
                    str(val_str).strip()
                    for val_str in opt_input.get("values", [])
                    if str(val_str).strip()
                ]
                value_rows = [VariantValue(option=option, value=val) for val in raw_values]
                for val_obj in VariantValue.objects.bulk_create(value_rows):
                    created_values_map[val_obj.value.lower()] = val_obj

            sku_rows, sku_value_names = [], []
            for sku_input in sku_items:
                if not isinstance(sku_input, dict):
                    continue
                price_override = sku_input.get("price_override")
                discount_price = sku_input.get("discount_price")
                sku_rows.append(
                    ProductItem(
                        product=product,
                        stock_qty=max(int(sku_input.get("stock_qty", 0)), 0),
                        price_override=Decimal(str(price_override)) if price_override not in (None, "") else None,
                        bonanza_mode=bool(sku_input.get("bonanza_mode", False)),
                        discount_price=Decimal(str(discount_price)) if discount_price not in (None, "") else None,
                        is_active=bool(sku_input.get("is_active", True)),
                    )
                )
                sku_value_names.append(sku_input.get("value_names", []))

            created_skus = ProductItem.objects.bulk_create(sku_rows)

            through = ProductItem.variant_values.through
            link_rows = []
            for item_sku, frontend_value_names in zip(created_skus, sku_value_names):
                seen = set()
                for val_name in frontend_value_names or []:
                    key = str(val_name).lower().strip()
                    val_obj = created_values_map.get(key)
                    if val_obj is not None and val_obj.pk not in seen:
                        seen.add(val_obj.pk)
                        link_rows.append(
                            through(productitem_id=item_sku.pk, variantvalue_id=val_obj.pk)
                        )
            if link_rows:
                through.objects.bulk_create(link_rows)

        # 6. Secondary Gallery Media (E-Commerce Only)
        if store_type != "restaurant" and gallery_images:
            for img_meta in gallery_images:
                if not isinstance(img_meta, dict):
                    continue
                f_key = img_meta.get("file_key")
                gallery_file = get_file_from_request(request_files, f_key)
                if f_key and gallery_file:
                    ProductImage.objects.create(
                        product=product,
                        image=gallery_file,
                        caption=str(img_meta.get("caption", "")).strip(),
                    )

        return product.id


@router.get("/inventory/add", response={200: dict})
def get_inventory_metadata(request, sub_category_id: int = None):
    if request.user.role != 'business':
        raise HttpError(403, "Unauthorized access channel.")
        
    try:
        profile = MerchantProfile.objects.get(user=request.user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile workspace not found.")

    is_restaurant = profile.business_type == 'restaurant'
    
    response_data = {
        "business_type": profile.business_type,
        "sub_categories": [],
        "brands": []
    }

    if is_restaurant:
        # Include all restaurant choices: meals, drinks, sides
        restaurant_primary_categories = ['restaurant']
        sub_cats = SubCategory.objects.filter(primary_category__in=restaurant_primary_categories)
        response_data["sub_categories"] = [{"id": sc.id, "name": sc.name} for sc in sub_cats]
    else:
        ecommerce_primary_categories = ['gadgets', 'apparel', 'appliances', 'beauty', 'general']
        sub_cats = SubCategory.objects.filter(primary_category__in=ecommerce_primary_categories)
        response_data["sub_categories"] = [{"id": sc.id, "name": sc.name} for sc in sub_cats]

        brands = Brand.objects.all()
        if sub_category_id:
            brands = brands.filter(sub_category_id=sub_category_id)
        response_data["brands"] = [{"id": b.id, "name": b.name} for b in brands]

    return 200, response_data

@router.post("/inventory/add", response={200: ProductAddSuccessResponse})
def add_product_api(request, data: Form[ProductSavePayloadSchema]):
    if request.user.role != 'business':
        raise HttpError(403, "Unauthorized access channel.")

    try:
        profile = MerchantProfile.objects.get(user=request.user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile workspace not found.")

    # --- Vendor Subscription & 7-Day Free Trial Validation ---
    access_status = evaluate_merchant_subscription_or_trial(profile)

    try:
        subscription = VendorSubscription.objects.select_related('plan').get(vendor_profile=profile)
    except VendorSubscription.DoesNotExist:
        subscription = None

    if subscription is not None:
        if not subscription.can_add_product:
            if not subscription.has_valid_access and not access_status.has_access:
                raise HttpError(403, "Your subscription or 7-day free trial has expired. Please subscribe to add products.")
            if access_status.is_on_trial:
                current_count = Product.objects.filter(vendor=profile).count()
                if current_count >= TRIAL_MAX_PRODUCTS:
                    raise HttpError(403, f"Free trial limit of {TRIAL_MAX_PRODUCTS} products reached. Upgrade your plan to add more.")
            else:
                plan_label = subscription.plan.name if subscription.plan else "current"
                raise HttpError(403, f"Product limit reached for your '{plan_label}' tier.")
    else:
        # No VendorSubscription row: allow if on an active 7-day free trial
        if not access_status.has_access:
            if access_status.reason == "trial_expired":
                raise HttpError(403, "Your 7-day free trial has expired. Please subscribe to add products.")
            raise HttpError(403, "No active subscription or free trial found. Please subscribe to add products.")

        if access_status.is_on_trial:
            current_count = Product.objects.filter(vendor=profile).count()
            if current_count >= TRIAL_MAX_PRODUCTS:
                raise HttpError(403, f"Free trial limit of {TRIAL_MAX_PRODUCTS} products reached. Upgrade your plan to add more.")

    is_restaurant = profile.business_type == 'restaurant'
    store_type = 'restaurant' if is_restaurant else 'ecommerce'

    # --- Auto-Assign or Validate Category based on SubCategory ---
    assigned_category = 'general'
    if data.sub_category_id and data.sub_category_id != "NONE" and data.sub_category_id != "CUSTOM":
        try:
            sub_cat_obj = SubCategory.objects.get(id=data.sub_category_id)
            assigned_category = sub_cat_obj.primary_category  # Automatically inherit parent category token!
        except SubCategory.DoesNotExist:
            raise HttpError(400, "Selected subcategory does not exist.")
    else:
        # Fallback defaults if no subcategory chosen
        assigned_category = 'restaurant' if is_restaurant else 'general'

    # Inject the resolved category back into data payload before passing to worker
    data.category = assigned_category

    # Safe JSON decoding for arrays
    try:
        parsed_variant_options = json.loads(data.variant_options) if data.variant_options else []
        parsed_sku_items = json.loads(data.sku_items) if data.sku_items else []
        parsed_gallery_images = json.loads(data.gallery_images) if data.gallery_images else []
        parsed_key_features = json.loads(data.key_features) if data.key_features else []
        parsed_menu_options = json.loads(data.menu_options) if data.menu_options else []
    except (json.JSONDecodeError, TypeError) as e:
        raise HttpError(400, f"Malformed matrix values encountered: {str(e)}")

    try:
        product_id = _sync_add_product_worker(
            user=request.user,
            profile=profile,
            store_type=store_type,
            data=data,
            key_features=parsed_key_features if not is_restaurant else [],
            variant_options=parsed_variant_options if not is_restaurant else [],
            sku_items=parsed_sku_items if not is_restaurant else [],
            gallery_images=parsed_gallery_images if not is_restaurant else [],
            request_files=request.FILES,
            menu_options=parsed_menu_options if is_restaurant else []
        )

        product_obj = Product.objects.get(id=product_id)
        base_host = f"{request.scheme}://{request.get_host()}"
        absolute_image_url = f"{base_host}{product_obj.image.url}" if product_obj.image else None

        return 200, {
            "status": "success",
            "message": f"Successfully published {data.name}.",
            "product_id": product_id,
            "image_url": absolute_image_url
        }

    except Exception:
        logger.exception("Complex catalog submission failed.")
        raise HttpError(500, "Unable to construct product lifecycle rows.")

# =========================================================================
# 4. EDIT INVENTORY ITEM
# =========================================================================

def _sync_edit_product_worker(
    user,
    profile,
    product,
    store_type,
    data,
    key_features,
    variant_options,
    sku_items,
    gallery_images,
    menu_options,
    request_files,
):
    def safe_json_parse(val, default):
        if not val:
            return default
        if isinstance(val, (list, dict)):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return default
        return default

    key_features = safe_json_parse(key_features, [])
    variant_options = safe_json_parse(variant_options, [])
    sku_items = safe_json_parse(sku_items, [])
    gallery_images = safe_json_parse(gallery_images, [])
    menu_options = safe_json_parse(menu_options, [])

    def get_file_from_request(files_obj, key):
        if not files_obj:
            return None
        if hasattr(files_obj, "get"):
            return files_obj.get(key)
        if isinstance(files_obj, (list, tuple)):
            for f in files_obj:
                if getattr(f, "name", None) == key or str(f).endswith(key):
                    return f
            return files_obj[0] if files_obj else None
        return None

    with transaction.atomic():
        # 1. Derive base category from MerchantProfile
        if store_type == "restaurant" or getattr(profile, "business_type", "") == "restaurant":
            resolved_category = "restaurant"
        else:
            raw_cat = (
                getattr(profile, "ecommerce_category", None)
                or getattr(profile, "ecommerce_sub_category", None)
                or getattr(data, "category", None)
                or "general"
            )
            resolved_category = str(raw_cat).lower().strip() or "general"

        # 2. Update primary core settings
        product.name = data.name.strip()
        product.description = (data.description or "").strip()
        product.key_features = key_features
        product.price = data.price
        product.category = resolved_category
        product.bonanza_mode = bool(data.bonanza_mode)
        product.discount_price = data.discount_price
        product.is_available = bool(data.is_available)

        primary_file = get_file_from_request(request_files, data.image_key or "image")
        if primary_file:
            product.image = primary_file

        # 3. Subcategories (Restaurants & E-Commerce)
        sub_category_selection = str(data.sub_category_id or "").strip()
        if sub_category_selection == "CUSTOM" and (data.new_sub_category_name or "").strip():
            sub_cat = _get_or_create_with_unique_slug(
                SubCategory,
                lookup_kwargs={
                    "name__iexact": data.new_sub_category_name.strip(),
                    "primary_category": resolved_category,
                },
                base_name=data.new_sub_category_name,
                extra_defaults={"name": data.new_sub_category_name.strip()},
            )
            product.sub_category = sub_cat
        elif sub_category_selection.isdigit():
            product.sub_category_id = int(sub_category_selection)
        else:
            product.sub_category = None

        # 4. Brands (E-Commerce Only)
        if store_type != "restaurant":
            brand_selection = str(data.brand_selection or "NONE").strip()
            if brand_selection == "CUSTOM" and (data.new_brand_name or "").strip():
                brand = _get_or_create_with_unique_slug(
                    Brand,
                    lookup_kwargs={"name__iexact": data.new_brand_name.strip()},
                    base_name=data.new_brand_name,
                    extra_defaults={
                        "name": data.new_brand_name.strip(),
                        "created_by": user,
                    },
                )
                if product.sub_category:
                    brand.sub_categories.add(product.sub_category)
                product.brand = brand
            elif brand_selection.isdigit():
                product.brand_id = int(brand_selection)
            else:
                product.brand = None

        product.save()

        # 5. Photo Gallery Preservation
        if gallery_images is not None:
            old_images = {img.id: img for img in ProductImage.objects.filter(product=product)}
            preserved_ids = []
            images_to_bulk_update = []

            for img_meta in gallery_images:
                if not isinstance(img_meta, dict):
                    continue
                file_key = img_meta.get("file_key")
                caption_str = str(img_meta.get("caption", "")).strip()
                existing_id = _to_int_or_none(img_meta.get("id"))

                gallery_file = get_file_from_request(request_files, file_key) if file_key else None

                if file_key and gallery_file:
                    new_img = ProductImage.objects.create(
                        product=product,
                        image=gallery_file,
                        caption=caption_str,
                    )
                    preserved_ids.append(new_img.id)
                elif existing_id and existing_id in old_images:
                    old_img = old_images[existing_id]
                    old_img.caption = caption_str
                    images_to_bulk_update.append(old_img)
                    preserved_ids.append(old_img.id)

            if images_to_bulk_update:
                ProductImage.objects.bulk_update(images_to_bulk_update, ["caption"])

            ProductImage.objects.filter(product=product).exclude(id__in=preserved_ids).delete()

        # 6. Restaurant Menu Options (Restaurant Only)
        if store_type == "restaurant" and menu_options is not None:
            existing_options = {opt.id: opt for opt in MenuItemOption.objects.filter(product=product)}
            processed_option_ids = []

            for opt_input in menu_options:
                if not isinstance(opt_input, dict):
                    continue
                opt_id = _to_int_or_none(opt_input.get("id"))
                opt_name = str(opt_input.get("name", "")).strip()
                opt_price = opt_input.get("price")
                is_available = bool(opt_input.get("is_available", True))

                if opt_name and opt_price not in (None, ""):
                    parsed_price = Decimal(str(opt_price))

                    if opt_id and opt_id in existing_options:
                        menu_opt = existing_options[opt_id]
                        menu_opt.name = opt_name
                        menu_opt.price = parsed_price
                        menu_opt.is_available = is_available
                        menu_opt.save()
                        processed_option_ids.append(menu_opt.id)
                    else:
                        menu_opt = MenuItemOption.objects.create(
                            product=product,
                            name=opt_name,
                            price=parsed_price,
                            is_available=is_available,
                        )
                        processed_option_ids.append(menu_opt.id)

            MenuItemOption.objects.filter(product=product).exclude(id__in=processed_option_ids).delete()

        # 7. Dynamic Variation & SKU Synthesis (E-Commerce Only)
        elif store_type != "restaurant" and sku_items is not None:
            if variant_options is not None:
                active_option_ids = []
                for opt_input in variant_options:
                    if not isinstance(opt_input, dict):
                        continue
                    opt_name = str(opt_input.get("name", "")).strip()
                    if not opt_name:
                        continue
                    option, _ = VariantOption.objects.get_or_create(product=product, name=opt_name)
                    active_option_ids.append(option.id)

                    active_val_ids = []
                    for val_str in opt_input.get("values", []):
                        clean_val = str(val_str).strip()
                        if not clean_val:
                            continue
                        val_obj, _ = VariantValue.objects.get_or_create(option=option, value=clean_val)
                        active_val_ids.append(val_obj.id)

                    VariantValue.objects.filter(option=option).exclude(id__in=active_val_ids).delete()

                VariantOption.objects.filter(product=product).exclude(id__in=active_option_ids).delete()

            available_values = VariantValue.objects.filter(option__product=product).select_related("option")
            flat_values_pool = {str(v.value).lower().strip(): v for v in available_values}

            existing_items = {item.id: item for item in ProductItem.objects.filter(product=product)}
            processed_item_ids = []

            for sku_input in sku_items:
                if not isinstance(sku_input, dict):
                    continue
                sku_id = _to_int_or_none(sku_input.get("id") or sku_input.get("item_id"))
                frontend_value_names = sku_input.get("value_names", [])

                stock_qty = max(int(sku_input.get("stock_qty") or 0), 0)
                raw_price = str(sku_input.get("price_override") or "").strip()
                price_override = Decimal(raw_price) if raw_price else None

                raw_discount = str(sku_input.get("discount_price") or "").strip()
                discount_price = Decimal(raw_discount) if raw_discount else None

                if sku_id and sku_id in existing_items:
                    item_sku = existing_items[sku_id]
                    item_sku.stock_qty = stock_qty
                    item_sku.price_override = price_override
                    item_sku.bonanza_mode = bool(sku_input.get("bonanza_mode", False))
                    item_sku.discount_price = discount_price
                    item_sku.is_active = bool(sku_input.get("is_active", True))
                    item_sku.save()
                    processed_item_ids.append(item_sku.id)
                else:
                    item_sku = ProductItem.objects.create(
                        product=product,
                        stock_qty=stock_qty,
                        price_override=price_override,
                        bonanza_mode=bool(sku_input.get("bonanza_mode", False)),
                        discount_price=discount_price,
                        is_active=bool(sku_input.get("is_active", True)),
                    )
                    processed_item_ids.append(item_sku.id)

                matched_relations = []
                seen_val_ids = set()
                for val_name in frontend_value_names or []:
                    key = str(val_name).lower().strip()
                    val_obj = flat_values_pool.get(key)
                    if val_obj is not None and val_obj.pk not in seen_val_ids:
                        seen_val_ids.add(val_obj.pk)
                        matched_relations.append(val_obj)

                item_sku.variant_values.set(matched_relations)

            ProductItem.objects.filter(product=product).exclude(id__in=processed_item_ids).update(is_active=False)

        return product.id
    
@router.post("/inventory/edit/{product_id}", response={200: dict})
def edit_product_api(request, product_id: str, data: ProductSavePayloadSchema = Form(...)):
    if request.user.role != 'business':
        raise HttpError(403, "Unauthorized access channel.")
        
    try:
        profile = MerchantProfile.objects.get(user=request.user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile workspace not found.")

    try:
        product = Product.objects.select_related('vendor').get(id=product_id, vendor=profile)
    except Product.DoesNotExist:
        raise HttpError(404, "Product line item not found.")

    store_type = 'restaurant' if profile.business_type == 'restaurant' else 'ecommerce'

    # Strict JSON payload decoding including menu options
    try:
        parsed_variant_options = json.loads(data.variant_options) if data.variant_options else None
        parsed_sku_items = json.loads(data.sku_items) if data.sku_items else None
        parsed_gallery_images = json.loads(data.gallery_images) if data.gallery_images else None
        parsed_key_features = json.loads(data.key_features) if data.key_features else []
        parsed_menu_options = json.loads(data.menu_options) if data.menu_options else None
    except (json.JSONDecodeError, TypeError) as e:
        raise HttpError(400, f"Failed to decode form array values: {str(e)}")

    try:
        _sync_edit_product_worker(
            request.user, 
            profile, 
            product, 
            store_type, 
            data, 
            parsed_key_features,
            parsed_variant_options, 
            parsed_sku_items, 
            parsed_gallery_images, 
            parsed_menu_options, # Pass parsed menu options
            request.FILES
        )
        return 200, {
            "status": "success", 
            "message": f"Successfully updated changes for {data.name}."
        }
    except HttpError:
        raise
    except Exception as e:
        logger.exception(f"Operational fault during product edit: {product_id}")
        raise HttpError(500, "Unable to complete inventory updates.")

@router.delete("/inventory/delete/{product_id}")
def delete_merchant_product(request, product_id: str):
    """
    Safely deletes a product specs sheet alongside its child SKU blocks, 
    options tags, and secondary disk photo assets in a single atomic wave.
    """
    # 1. Fetch vendor profile context safely
    try:
        profile = MerchantProfile.objects.get(user=request.user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile workspace not found.")
    
    try:
        # 2. Assert this item belongs explicitly to the requesting merchant node
        product = Product.objects.get(id=product_id, vendor=profile)
    except Product.DoesNotExist:
        raise HttpError(404, "Target product listing not found or unauthorized.")

    # 3. Handle data cleanup inside a synchronous database transaction worker block
    def _sync_delete_product_worker(target_product):
        with transaction.atomic():
            # A. Extract and remove secondary gallery image file binaries from storage disk
            gallery_images = ProductImage.objects.filter(product=target_product)
            for img in gallery_images:
                if img.image and os.path.isfile(img.image.path):
                    try:
                        os.remove(img.image.path)
                    except Exception as disk_err:
                        print(f"Disk file clearing exception trace skipped: {disk_err}")
            
            # B. Remove the primary hero thumbnail file binary from disk storage safely
            if target_product.image and os.path.isfile(target_product.image.path):
                try:
                    os.remove(target_product.image.path)
                except Exception as disk_err:
                    print(f"Primary media file clearing exception trace skipped: {disk_err}")

            # C. Execute database cascading purge tracking trees
            # Note: ForeignKey models with on_delete=models.CASCADE (like ProductItem, VariantOption)
            # will clear out implicitly, cleaning up intermediate bridge links beautifully!
            target_product.delete()

    # Dispatch to thread pool execution lane
    _sync_delete_product_worker(product)

    return {
        "status": "success",
        "message": f"Successfully deleted '{product.name}' and all linked variation allocations."
    }

# =========================================================================
# 5. RETRIEVE SINGLE INVENTORY ITEM DETAILS
# =========================================================================
@router.get("/inventory/detail/{product_id}", response={200: ProductDetailPayloadSchema})
def get_product_detail_api(request, product_id: str):
    """Retrieves full specification details for a single inventory object context safely."""
    try:
        # Offload the entire read-and-serialize dance to a highly-efficient database thread worker
        result_data = _sync_get_product_detail_worker(
            request.user, product_id, request
        )
        return 200, {"status": "success", "data": result_data}
        
    except Product.DoesNotExist:
        raise HttpError(404, "Inventory item trace not found.")
    except Exception as e:
        logger.exception(f"Failed to resolve detailed inventory trace: {product_id}")
        raise HttpError(500, "Unable to extract specification details.")


def _sync_get_product_detail_worker(user, product_id, request):
    # Enclosing this in a read-only transaction block ensures absolute data isolation
    with transaction.atomic():
        product = Product.objects.select_related('sub_category', 'brand', 'vendor').get(
            id=product_id, 
            vendor__user=user
        )
        
        # Build absolute image location string safely in synchronous context
        base_host = f"{request.scheme}://{request.get_host()}"
        primary_image_url = f"{base_host}{product.image.url}" if product.image else None

        # 1. Gather all variant options along with values
        variant_options = []
        options = VariantOption.objects.filter(product=product).prefetch_related('values')
        for option in options:
            variant_options.append({
                "name": option.name,
                "values": [val.value for val in option.values.all()]
            })

        # 2. Gather SKUs along with value mappings
        sku_items = []
        items = ProductItem.objects.filter(product=product).prefetch_related('variant_values')
        for item in items:
            sku_items.append({
                "id": item.id,
                "stock_qty": item.stock_qty,
                "price_override": item.price_override,
                "bonanza_mode": item.bonanza_mode,
                "discount_price": item.discount_price,
                "is_active": item.is_active,
                "value_names": [v.value for v in item.variant_values.all()]
            })

        # 3. Gather secondary media assets cleanly (Resolves the main bug!)
        gallery_images = []
        images = ProductImage.objects.filter(product=product)
        for img in images:
            gallery_images.append({
                "id": img.id,
                "image": f"{base_host}{img.image.url}" if img.image else "",
                "caption": img.caption or ""
            })

        # 4. Return clean primitive dict payload for automatic schema generation
        return {
            "id": product.id,
            "name": product.name,
            "description": product.description or "",
            "key_features": product.key_features if isinstance(product.key_features, list) else [],
            "price": product.price,
            "category": product.category,
            "sub_category": {"id": product.sub_category.id, "name": product.sub_category.name} if product.sub_category else None,
            "brand": {"id": product.brand.id, "name": product.brand.name} if product.brand else None,
            "bonanza_mode": product.bonanza_mode,
            "discount_price": product.discount_price,
            "is_available": product.is_available,
            "image": primary_image_url,
            "variant_options": variant_options,
            "sku_items": sku_items,
            "gallery_images": gallery_images
        }

# =========================================================================
# 6. BATCH SKU MATRIX PRICING MATRIX COUPLER
# =========================================================================
@router.post("/inventory/variant-pricing/update", auth=JWTAuth())
def update_variant_pricing_matrix_api(request, data: List[BatchSKUOverrideUpdateSchema]):
    profile = MerchantProfile.objects.get(user=request.user)
    try:
        _sync_pricing_matrix_worker(profile, data)
        return {"status": "success", "message": "SKU variant matrices updated successfully."}
    except ProductItem.DoesNotExist:
        return JsonResponse({"detail": "Access denied for one or more requested variant entries."}, status=403)
    except Exception as e:
        return JsonResponse({"detail": f"Batch fault: {str(e)}"}, status=500)

def _sync_pricing_matrix_worker(profile, items_data):
    with transaction.atomic():
        for item in items_data:
            target_sku = ProductItem.objects.select_related('product').get(
                id=item.item_id, 
                product__vendor=profile
            )
            target_sku.stock_qty = item.stock_qty
            target_sku.price_override = Decimal(str(item.price_override)) if item.price_override else None
            target_sku.is_active = item.is_active
            target_sku.save()

@router.post("/inventory/toggle-status/{product_id}")
def toggle_product_availability_api(request, product_id: str):
    """
    Instantly flips the on/off stock status visibility for a product line item 
    within the calling merchant's multi-tenant domain boundary safely.
    """
    try:
        profile = MerchantProfile.objects.get(user=request.user)
    except MerchantProfile.DoesNotExist:
        return {"status": "error", "message": "Merchant profile profile layer trace missing."}

    try:
        product = Product.objects.get(id=product_id, vendor=profile)
    except Product.DoesNotExist:
        # Django-Ninja catches standard dictionary returns or raw Ninja exceptions cleanly
        return {"status": "error", "message": "Inventory tracking trace not found under current profile."}

    # 1. Mutate operational toggle boolean parameters flags
    product.is_available = not product.is_available
    
    # 2. Use async database save to ensure non-blocking loop handling
    product.save(update_fields=['is_available'])
    
    status_msg = "Live & Available" if product.is_available else "Marked Out of Stock"
    return {
        "status": "success", 
        "message": f"Stock status updated to: {status_msg}."
    }

@router.get("/profile", response={200: BusinessProfileResponse})
def merchant_profile_api(request):
    """
    Synchronously retrieves the merchant profile records and linked storefront configuration matrix.
    If no record exists, it safely boots an empty placeholder mapping row.
    """
    user = request.user

    if not user or user.is_anonymous or user.role != 'business':
        raise HttpError(403, "Access Denied: Restricted to registered corporate business merchant profiles.")

    try:
        # Include 'user__base_location' in select_related to prevent async lazy loading errors
        profile = MerchantProfile.objects.select_related('user', 'user__base_location', 'storefront_config').get(user=user)
    except MerchantProfile.DoesNotExist:
        fallback_name = f"{user.email.split('@')[0].capitalize()} Venture"
        
        profile = MerchantProfile.objects.acreate(
            id=f"VEN-{generate_alphanumeric_id(8)}",
            user=user,
            company_name=fallback_name,
            default_pickup_address='',
            business_type='other'
        )
        profile = MerchantProfile.objects.select_related('user', 'user__base_location', 'storefront_config').get(id=profile.id)
    except Exception as e:
        logger.exception("Failed to compile profile data attributes matrix.")
        raise HttpError(500, f"Profile asset resolution fault: {str(e)}")

    config = getattr(profile, 'storefront_config', None)
    hero_image_url = config.hero_image.url if (config and config.hero_image) else None

    # Safely resolve base location string name
    base_loc_name = str(profile.user.base_location.name) if (profile.user and profile.user.base_location) else ""

    return 200, {
        "id": profile.id,
        "company_name": profile.company_name,
        "business_type": profile.business_type or "other",
        "ecommerce_sub_category": profile.ecommerce_sub_category,
        "phone_number": profile.user.phone_number or "",
        "base_location": user.base_location_id,
        "base_location_name": base_loc_name,
        "street_address": profile.user.street_address or "",
        "is_approved": profile.is_approved,
        "is_currently_open": getattr(profile, 'is_currently_open', False),
        "opening_time": profile.opening_time.strftime("%H:%M") if profile.opening_time else "00:00",
        "closing_time": profile.closing_time.strftime("%H:%M") if profile.closing_time else "00:00",
        "latitude": float(profile.user.latitude) if profile.user.latitude else None,
        "longitude": float(profile.user.longitude) if profile.user.longitude else None,
        "store_slug": getattr(config, 'store_slug', None),
        "tagline": getattr(config, 'tagline', None),
        "brand_bio": getattr(config, 'brand_bio', None),
        "instagram_handle": getattr(config, 'instagram_handle', None),
        "twitter_handle": getattr(config, 'twitter_handle', None),
        "hero_image": hero_image_url
    }

def _sync_atomic_profile_writer(user, input_data, hero_image, profile_instance):
    """
    Executes database mutations and related child model creation flags safely inside an atomic block.
    """
    with transaction.atomic():
        def parse_time_flexibly(time_str: str):
            time_str = time_str.strip()
            for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
                try:
                    return datetime.strptime(time_str, fmt).time()
                except ValueError:
                    continue
            raise ValueError(f"Time format matching rules completely exhausted for input: {time_str}")

        try:
            open_time = parse_time_flexibly(input_data.opening_time)
            close_time = parse_time_flexibly(input_data.closing_time)
        except ValueError as time_err:
            raise HttpError(400, str(time_err))

        user.latitude = input_data.latitude
        user.longitude = input_data.longitude
        user.street_address = input_data.street_address.strip() if input_data.street_address else None
        user.phone_number = input_data.phone_number.strip() if input_data.phone_number else None
        
        # --- Robust Base Location ID Assignment ---
        if input_data.base_location:
            try:
                user.base_location_id = int(input_data.base_location)
            except (ValueError, TypeError):
                user.base_location_id = None
        else:
            user.base_location_id = None

        # Explicitly save the User model fields
        user.save(update_fields=['latitude', 'longitude', 'street_address', 'base_location', 'phone_number'])

        # --- Update MerchantProfile ---
        profile_instance.company_name = input_data.company_name.strip()
        profile_instance.business_type = input_data.business_type.strip().lower()
        profile_instance.opening_time = open_time
        profile_instance.closing_time = close_time

        sub_cat = getattr(input_data, 'ecommerce_sub_category', None)
        profile_instance.ecommerce_sub_category = sub_cat.strip() if (sub_cat and sub_cat.strip()) else None
        profile_instance.save()

        # --- Update StorefrontConfig ---
        store_config, _ = StorefrontConfig.objects.get_or_create(profile=profile_instance)
        
        store_config.tagline = (input_data.tagline or '').strip()
        store_config.brand_bio = (input_data.brand_bio or '').strip()
        
        if input_data.instagram_handle:
            store_config.instagram_handle = input_data.instagram_handle.strip().split('/')[-1].replace('@', '')
        if input_data.twitter_handle:
            store_config.twitter_handle = input_data.twitter_handle.strip().split('/')[-1].replace('@', '')

        if hero_image:
            store_config.hero_image = hero_image

        store_config.save()

        return profile_instance
      
@router.post("/dashboard/profile/update", response={200: BusinessProfileResponse})
def update_profile_api(
    request, 
    data: UpdateBusinessProfileInput = Form(...), 
    hero_image: Optional[UploadedFile] = File(None)
):
    """
    Processes incoming brand updates and profile parameters asynchronously.
    """
    user = request.user

    if not user or user.is_anonymous or user.role != 'business':
        raise HttpError(403, "Authentication mismatch: Account tier lacks corporate editing clearance.")

    try:
        profile = MerchantProfile.objects.get(user=user)
    except MerchantProfile.DoesNotExist:
        fallback_name = f"{user.email.split('@')[0].capitalize()} Venture"
        profile = MerchantProfile.objects.create(
            id=f"ven-{generate_alphanumeric_id(8)}",
            user=user,
            company_name=fallback_name,
            default_pickup_address='',
            business_type='other'
        )

    try:
        updated_profile = _sync_atomic_profile_writer(
            user=user,
            input_data=data,
            hero_image=hero_image,
            profile_instance=profile
        )
    except HttpError as gateway_err:
        raise gateway_err
    except Exception as e:
        logger.exception("Persistence layer transaction processing broken.")
        raise HttpError(500, f"An exception occurred inside the transaction loop: {str(e)}")

    # Include 'user__base_location' in select_related here as well
    complete_profile = MerchantProfile.objects.select_related('user', 'user__base_location', 'storefront_config').get(id=updated_profile.id)
    config = getattr(complete_profile, 'storefront_config', None)
    hero_image_url = config.hero_image.url if (config and config.hero_image) else None

    # Resolve the human-readable location name safely
    base_loc_name = ""
    if complete_profile.user and complete_profile.user.base_location_id:
        try:
            area = CoverageArea.objects.select_related('state').get(id=complete_profile.user.base_location_id)
            base_loc_name = f"{area.name}, {area.state.name}"
        except CoverageArea.DoesNotExist:
            pass

    return 200, {
        "id": complete_profile.id,
        "company_name": complete_profile.company_name,
        "business_type": complete_profile.business_type,
        "ecommerce_sub_category": complete_profile.ecommerce_sub_category,
        "phone_number": complete_profile.user.phone_number,
        "street_address": complete_profile.user.street_address if complete_profile.user.street_address else None,
        "base_location": user.base_location_id,
        "is_approved": complete_profile.is_approved,
        "is_currently_open": getattr(complete_profile, 'is_currently_open', False),
        "opening_time": complete_profile.opening_time.strftime("%H:%M") if complete_profile.opening_time else "00:00",
        "closing_time": complete_profile.closing_time.strftime("%H:%M") if complete_profile.closing_time else "00:00",
        "latitude": float(complete_profile.user.latitude) if complete_profile.user.latitude else None,
        "longitude": float(complete_profile.user.longitude) if complete_profile.user.longitude else None,
        "store_slug": getattr(config, 'store_slug', None),
        "tagline": getattr(config, 'tagline', None),
        "brand_bio": getattr(config, 'brand_bio', None),
        "instagram_handle": getattr(config, 'instagram_handle', None),
        "twitter_handle": getattr(config, 'twitter_handle', None),
        "hero_image": hero_image_url
    }

@router.get("/active-orders", response={200: ActiveOrdersListSchema})
def get_merchant_active_orders(request):
    user = request.user

    # 1. Guardrail: Validate role
    if hasattr(user, 'role') and user.role != 'business':
        raise HttpError(403, "Access restricted strictly to verified business vendor tokens.")

    # 2. RESOLVE MERCHANT PROFILE:
    try:
        merchant = MerchantProfile.objects.get(user=user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Associated merchant profile not found for this user.")

    # 3. ONE query for the orders, plus ONE prefetch for every order's items and their
    #    products. (The old code ran an items query - and sometimes two extra order lookups -
    #    for every order in the list.)
    items_prefetch = Prefetch(
        'items',
        queryset=MarketplaceOrderItem.objects.select_related('product', 'product_item__product'),
    )
    queryset = list(
        LogisticsOrder.objects.select_related(
            'receiver__user', 'assigned_rider__user', 'marketplace_order'
        ).prefetch_related(
            Prefetch('marketplace_order__items', queryset=items_prefetch.queryset)
        ).filter(
            sender=merchant,
            status__in=['pending', 'acknowledged', 'assigned', 'in_transit']
        ).order_by('-created_at')
    )

    # Legacy rows linked only through MarketplaceOrder.logistics_dispatch: resolve them all at once.
    unlinked_ids = [o.id for o in queryset if o.marketplace_order_id is None]
    reverse_map = {}
    if unlinked_ids:
        for m in MarketplaceOrder.objects.filter(logistics_dispatch_id__in=unlinked_ids).prefetch_related(items_prefetch):
            reverse_map[m.logistics_dispatch_id] = m

    orders_list = []
    for order in queryset:
        # Safely resolve receiver phone number
        receiver_phone = ""
        if order.receiver and order.receiver.user:
            receiver_phone = getattr(order.receiver.user, 'phone_number', "") or ""

        # Safely resolve assigned rider details if present
        rider_info = None
        if order.assigned_rider:
            rider_user = getattr(order.assigned_rider, 'user', None)
            rider_info = {
                "id": str(order.assigned_rider.id),
                "name": f"{getattr(rider_user, 'first_name', '')} {getattr(rider_user, 'last_name', '')}".strip() or "Assigned Rider",
                "phone_number": getattr(rider_user, 'phone_number', "") or getattr(order.assigned_rider, 'phone_number', "")
            }

        mkt = order.marketplace_order or reverse_map.get(order.id)

        items_data = []
        computed_sub = Decimal('0.00')

        if mkt:
            for item in mkt.items.all():  # served from the prefetch cache: no query
                name = "Item"
                if item.product:
                    name = item.product.name
                elif item.product_item and item.product_item.product:
                    name = item.product_item.product.name

                line_total = item.get_total_item_price()
                computed_sub += line_total

                items_data.append({
                    "name": name,
                    "quantity": item.quantity,
                    "price_at_order": float(item.price_at_order),
                    "total_item_price": float(line_total)
                })

            # Fallback to model field if populated
            if mkt.subtotal and mkt.subtotal > 0:
                computed_sub = mkt.subtotal

        delivery_fee = float(order.delivery_fee)
        subtotal = float(computed_sub)
        total_price = subtotal + delivery_fee
        payment_reference = getattr(mkt, 'payment_reference', None) if mkt else None
        marketplace_status = getattr(mkt, 'status', None) if mkt else str(order.status)
        marketplace_order_id = str(mkt.id) if mkt else None

        orders_list.append({
            "id": order.id,
            "order_id": order.marketplace_order_id,
            "marketplace_order_id": marketplace_order_id,
            "order_reference": order.order_reference,

            "sender_name": order.sender_name,
            "sender_phone": order.sender_phone,
            "pickup_address": order.pickup_address,
            "receiver_name": order.receiver_name,
            "receiver_phone": receiver_phone,
            "delivery_address": order.delivery_address,

            "package_type": order.package_type,
            "package_description": order.package_description or "Marketplace Package",
            "items": items_data,

            "subtotal": subtotal,
            "delivery_fee": delivery_fee,
            "total_price": total_price,
            "payment_reference": payment_reference,
            "is_cash_on_delivery": order.is_cash_on_delivery,
            "cod_amount_to_collect": float(order.cod_amount_to_collect),

            "marketplace_status": marketplace_status,
            "status": str(order.status).lower(),

            "assigned_rider": rider_info,
            "created_at": order.created_at,
            "updated_at": order.updated_at
        })

    return 200, {
        "status": "success",
        "active_orders": orders_list
    }

@router.get("/order-summary/{order_id}", auth=JWTAuth())
def merchant_order_summary_api(request, order_id: str):
    user = request.user
    
    if hasattr(user, 'role') and user.role != 'business':
        raise HttpError(403, "Access restricted strictly to verified business vendor tokens.")

    try:
        merchant = MerchantProfile.objects.get(user=user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Associated merchant profile not found for this user.")

    try:
        order = MarketplaceOrder.objects.select_related('vendor').get(
            id=order_id, 
            vendor=merchant
        )
    except MarketplaceOrder.DoesNotExist:
        raise HttpError(404, "Order transaction instance missing or unauthorized.")

    items_cursor = MarketplaceOrderItem.objects.filter(order=order).select_related(
        'product_item',
        'product_item__product', 
        'product'
    )
    
    purchased_items = []
    for item in items_cursor:
        if item.product_item and hasattr(item.product_item, 'product') and item.product_item.product:
            p_name = item.product_item.product.name
        elif item.product:
            p_name = item.product.name
        else:
            p_name = "Unknown Item"

        purchased_items.append({
            "product_name": str(p_name),
            "quantity": int(item.quantity),
            "unit_price": float(item.price_at_order),
            "total_item_price": float(item.price_at_order * item.quantity)
        })

    # Fallback or compute subtotal if 0.00
    subtotal = float(order.subtotal) if order.subtotal > 0 else sum(i['total_item_price'] for i in purchased_items)

    return {
        "status": "success",
        "order_reference": str(order.id),
        "items": purchased_items,
        "subtotal": subtotal,
        "delivery_fee": float(order.delivery_fee),
        "total_amount": float(order.total_price) if order.total_price > 0 else (subtotal + float(order.delivery_fee))
    }

@router.get("/notifications", response={200: list})
def get_merchant_notifications(request):
    """
    Fetch recent system broadcasts, announcements, and order notifications for the authenticated merchant.
    """
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication required.")

    # Fetch relevant broadcasts targeting 'all' or 'merchants'
    # Alternatively, you can query a custom MerchantNotification model if you save them.
    broadcasts = SystemBroadcast.objects.filter(
        target_audience__in=['all', 'merchants', 'vendors']
    ).order_by('-created_at')[:20]

    notification_list = []
    for b in broadcasts:
        notification_list.append({
            "id": f"broadcast-{b.id}",
            "type": "broadcast",
            "message": f"<b>{b.title}</b><br/><span class='text-gray-600 font-normal'>{b.message}</span>",
            "timestamp": b.created_at.strftime("%H:%M"),
            "read": getattr(b, 'is_read', False)
        })

    return notification_list

@router.post("/notifications/mark-read/{broadcast_id}", response={200: dict})
def mark_broadcast_as_read(request, broadcast_id: int):
    """
    Mark a specific broadcast notification as read.
    """
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication required.")

    broadcast = get_object_or_404(SystemBroadcast, id=broadcast_id)
    broadcast.is_read = True
    broadcast.save(update_fields=['is_read'])

    return {"status": "success", "message": "Notification marked as read."}

@router.post("/profile/update-coordinates")
def update_merchant_coordinates_api(request, data: CoordinateUpdatePayload):
    """
    Synchronously links real-time device coordinate geometries 
    and address metadata to the merchant's MerchantProfile and User account.
    """
    user = request.user

    if not user or user.is_anonymous:
        raise HttpError(401, "Authentication credentials missing or invalid.")

    try:
        merchant_profile = user.merchant_profile
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile not found for this user account.")
    
    update_user_fields = []
    
    # Update user-level base location and address if provided
    if data.street_address is not None:
        user.street_address = data.street_address
        update_user_fields.append('street_address')

    if data.base_location is not None:
        user.base_location = data.base_location
        update_user_fields.append('base_location')

    # Update user-level GPS coordinates if provided
    if data.latitude is not None and data.longitude is not None:
        user.latitude = data.latitude
        user.longitude = data.longitude
        update_user_fields.extend(['latitude', 'longitude'])

    if update_user_fields:
        user.save(update_fields=update_user_fields)

    return {
        "status": "success",
        "message": "Merchant geospatial metrics synchronized successfully.",
        "data": {
            "latitude": float(data.latitude) if data.latitude is not None else None,
            "longitude": float(data.longitude) if data.longitude is not None else None,
            "base_location": data.base_location,
            "street_address": data.street_address
        }
    }


# =========================================================================
# SHIPPING PREFERENCE (who delivers: Aika riders, the vendor, or both)
# =========================================================================

def _shipping_preference_payload(vendor):
    pref = VendorShippingPreference.objects.filter(vendor=vendor).first()
    is_restaurant = str(vendor.business_type or '').lower() == 'restaurant'
    return {
        "fulfillment_type": pref.fulfillment_type if pref else 'platform',
        "allow_nationwide": pref.allow_nationwide if pref else False,
        "self_delivery_fee": float(pref.self_delivery_fee) if pref else 0.0,
        # Restaurants always use platform riders; the UI should hide the other options.
        "can_self_deliver": not is_restaurant,
    }


@router.get("/shipping-preference")
def get_shipping_preference(request):
    try:
        vendor = MerchantProfile.objects.get(user=request.user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile not found.")
    return {"status": "success", "data": _shipping_preference_payload(vendor)}

@router.post("/shipping-preference")
def update_shipping_preference(request, payload: ShippingPreferenceInput):
    try:
        vendor = MerchantProfile.objects.get(user=request.user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile not found.")

    if str(vendor.business_type or '').lower() == 'restaurant' and payload.fulfillment_type != 'platform':
        raise HttpError(400, "Restaurants are delivered by Aika riders only.")

    VendorShippingPreference.objects.update_or_create(
        vendor=vendor,
        defaults={
            'fulfillment_type': payload.fulfillment_type,
            'allow_nationwide': payload.allow_nationwide,
            'self_delivery_fee': payload.self_delivery_fee,
        },
    )
    return {"status": "success", "message": "Delivery preference saved.", "data": _shipping_preference_payload(vendor)}
