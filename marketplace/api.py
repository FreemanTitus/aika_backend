# marketplace/api.py
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.http import Http404, HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from ninja import Query, Router
from ninja.errors import HttpError
from weasyprint import HTML

from accounts.models import CustomUser
from accounts.utils import JWTAuth, compute_tariff_breakdown, verify_jwt_token
from customers.models import CustomerWallet, IndividualProfile
from merchant.models import MerchantProfile, StorefrontConfig
from marketplace.models import (
    Brand, MarketplaceOrder, MarketplaceOrderItem, MenuItemOption, Product,
    ProductImage, ProductItem, SubCategory, VariantOption,
)
from order.models import Order as LogisticOrder, UserCartItem
from order.services import process_order_referral_reward
from wallet.utils import PAID_ORDER_STATES, verify_order_payment

from .schemas import (
    CategoryFilterSchema, CheckoutRequestInput, OrderSummaryResponse,
    PublicProductPayloadSchema, QuoteRequestInput, QuoteRequestSchema,
    StorefrontFilters, SubCategoryFilterSchema,
)
from .utils import (
    AreaRequiredError, _calculate_distance_delivery_fee, apply_wallet_to_order, decide_fulfillment, ensure_payment_link,
    parse_clean_id, release_unpaid_order, sanitize_customer_coordinates, _resolve_buyer_area
)

router = Router(tags=["Marketplace Storefront"])

logger = logging.getLogger("marketplace")

MAX_LINE_QUANTITY = 99
MAX_CART_LINES = 50
MAX_ORDER_VALUE = Decimal("99999999.99")  # MarketplaceOrder money fields are max_digits=10


def _optional_user(request):
    """Resolves the bearer token on public endpoints without forcing authentication."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return verify_jwt_token(header[7:].strip())
    return None


def _clear_user_cart_for_order(order):
    """Removes the cart rows for this vendor once the order is paid (idempotent)."""
    customer = order.customer
    target_user = customer.user if customer and getattr(customer, "user", None) else None
    if not target_user:
        return
    cart_rows = UserCartItem.objects.filter(user=target_user)
    if order.vendor_id:
        cart_rows = cart_rows.filter(store_id__iexact=str(order.vendor_id))
    deleted_count, _ = cart_rows.delete()
    logger.info("Cleared %s cart item(s) for order %s", deleted_count, order.id)


def _post_payment_side_effects(order):
    """
    Cart cleanup and referral reward after a confirmed payment. Each step is
    isolated: a failing side effect must never turn a confirmed payment into an
    error response (and the payment stays confirmed regardless).
    """
    for step in (_clear_user_cart_for_order, process_order_referral_reward):
        try:
            with transaction.atomic():  # savepoint per step
                step(order)
        except Exception:
            logger.exception("Post-payment step %s failed for order %s", step.__name__, order.id)


async def _confirm_and_finalize(order_obj) -> bool:
    """
    True only if the order is genuinely paid. Never trusts client input: the
    verification always uses the reference stored on the order itself, and
    every post-payment status (processing, paid_unassigned, in_transit,
    completed) counts as already paid so races with the webhook are harmless.
    """
    if order_obj.status not in PAID_ORDER_STATES:
        if order_obj.status != MarketplaceOrder.OrderStatus.PENDING or not order_obj.payment_reference:
            return False
        ok = await sync_to_async(verify_order_payment)(
            str(order_obj.id), transaction_id=order_obj.payment_reference
        )
        if not ok:
            return False

    await sync_to_async(_post_payment_side_effects)(order_obj)
    return True


# =========================================================================
# 1. GET ALL VENDORS
# =========================================================================
@router.get("/vendors")
def get_marketplace_vendors_api(request, customer_location_id: Optional[int] = None):
    """
    Retrieves marketplace vendors. 
    Restaurants are restricted strictly to the customer's selected base location hub.
    E-commerce and other businesses remain globally visible.
    """
    now = timezone.now()
    
    # If customer_location_id is not passed as query param, try getting it from authenticated user
    if not customer_location_id and request.user and request.user.is_authenticated:
        if hasattr(request.user, 'base_location_id'):
            customer_location_id = request.user.base_location_id

    query_set = MerchantProfile.objects.filter(
        is_approved=True,
        is_active_store=True,
        vendorsubscription__is_active=True,
        vendorsubscription__subscription_expires_at__gt=now
    ).select_related(
        'user', 
        'user__base_location',
        'vendorsubscription', 
        'vendorsubscription__plan', 
        'storefront_config'
    ).order_by(
        '-vendorsubscription__plan__price', 
        '-rating', 
        '-review_count'
    )

    restaurants = []
    e_commerce = []
    other_businesses = []

    for vendor in query_set:
        vendor_loc_id = vendor.user.base_location_id if (vendor.user and vendor.user.base_location) else None
        base_loc = str(vendor.user.base_location.name) if (vendor.user and vendor.user.base_location) else ''
        
        active_sub = getattr(vendor, 'vendorsubscription', None)
        plan_name = active_sub.plan.name if active_sub and active_sub.plan else 'Standard'
        plan_price = float(active_sub.plan.price) if active_sub and active_sub.plan else 0.0
        is_premium = plan_price >= 15000

        storefront = getattr(vendor, 'storefront_config', None)
        hero_image_url = storefront.hero_image.url if (storefront and storefront.hero_image) else None
        store_slug = storefront.store_slug if storefront else None

        vendor_data = {
            "id": vendor.id,
            "company_name": vendor.company_name,
            "business_type": vendor.business_type,
            "ecommerce_sub_category": vendor.ecommerce_sub_category,
            "base_location": base_loc,
            "base_location_id": vendor_loc_id,
            "rating": float(vendor.rating) if vendor.rating else 0.0,
            "review_count": getattr(vendor, 'review_count', 0),
            "default_pickup_address": vendor.user.street_address or 'No Address Set',
            "phone_number": vendor.user.phone_number or '',
            "is_currently_open": getattr(vendor, 'is_currently_open', True),
            "opening_time": vendor.opening_time.strftime("%H:%M") if vendor.opening_time else "08:00",
            "closing_time": vendor.closing_time.strftime("%H:%M") if vendor.closing_time else "22:00",
            "subscription_tier": plan_name,
            "subscription_price": plan_price,
            "is_featured_recommendation": is_premium,
            "hero_image": hero_image_url,
            "store_slug": store_slug,
        }

        b_type = str(vendor.business_type).lower().strip()

        if b_type in ['restaurant']:
            # --- RESTAURANT GEOFENCING LOGIC ---
            # Only include restaurant if customer location matches the vendor's hub location
            if customer_location_id is not None:
                if vendor_loc_id == int(customer_location_id):
                    restaurants.append(vendor_data)
            else:
                # Fallback: if no location is selected yet, optionally show local default or skip
                # (You can choose to include all or none until they pick a hub)
                restaurants.append(vendor_data)
        elif b_type in ['ecommerce', 'e_commerce', 'general']:
            # --- E-COMMERCE: GLOBAL VISIBILITY ---
            e_commerce.append(vendor_data)
        else:
            other_businesses.append(vendor_data)

    return {
        "status": "success",
        "message": "Marketplace vendors retrieved successfully with regional fulfillment constraints.",
        "data": {
            "restaurants": restaurants,
            "e_commerce": e_commerce,
            "other_businesses": other_businesses
        }
    }

# =========================================================================
# 2. GET VENDOR STOREFRONT DETAILS
# =========================================================================
@router.get("/vendor/{vendor_id}")
def get_vendor_storefront_api(request, vendor_id: str, filters: StorefrontFilters = Query(...)):
    """
    Synchronously retrieves a specific vendor's digital storefront assets and reviews.
    Intelligently branches data structures for E-Commerce and Restaurant clients.
    """
    try:
        # Include 'user__base_location' to prevent lazy-loading database lookups
        vendor = MerchantProfile.objects.select_related('user', 'user__base_location').get(id=vendor_id, is_approved=True)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Approved vendor profile not found.")

    # Resolve last order history cleanly if user session context is authenticated
    # This endpoint is public, so the bearer token (if any) is read manually.
    # (The old query compared a CustomUser to profile FKs and always failed silently.)
    last_order_ref = ''
    viewer = _optional_user(request)
    if viewer:
        last_order = LogisticOrder.objects.filter(
            receiver__user=viewer,
            sender=vendor,
            status='completed'
        ).order_by('-created_at').first()
        if last_order:
            last_order_ref = last_order.order_reference or ''

    total_reviews = vendor.review_count if (vendor.review_count and vendor.review_count > 0) else 1
    
    ratings_matrix = {
        "rating_5_percentage": int(((vendor.count_5_stars or 0) / total_reviews) * 100),
        "rating_4_percentage": int(((vendor.count_4_stars or 0) / total_reviews) * 100),
        "rating_3_percentage": int(((vendor.count_3_stars or 0) / total_reviews) * 100),
        "rating_2_percentage": int(((vendor.count_2_stars or 0) / total_reviews) * 100),
        "rating_1_percentage": int(((vendor.count_1_stars or 0) / total_reviews) * 100),
    }

    reviews_list = []
    reviews_query = vendor.reviews.select_related('customer', 'customer__individual_profile').order_by('-created_at')[:15]
    for review in reviews_query:
        # Never expose reviewers' e-mail addresses on a public page.
        profile = getattr(review.customer, 'individual_profile', None)
        first = (getattr(profile, 'first_name', '') or '').strip()
        last = (getattr(profile, 'last_name', '') or '').strip()
        customer_name = f"{first} {last[:1]}.".strip() if first else "Anonymous Customer"

        reviews_list.append({
            "id": review.id,
            "rating": review.rating,
            "comment": review.comment,
            "customer_name": customer_name,
            "created_at": review.created_at.isoformat()
        })

    # Safely extract base location name
    base_loc_name = str(vendor.user.base_location.name) if (vendor.user and vendor.user.base_location) else ''

    base_data = {
        "vendor_id": vendor.id,
        "company_name": vendor.company_name,
        "business_type": vendor.business_type,
        "base_location": base_loc_name,
        "rating": float(vendor.rating) if vendor.rating else 0.0,
        "review_count": vendor.review_count,
        "is_currently_open": getattr(vendor, 'is_currently_open', True),
        "last_order_ref": last_order_ref,
        "ratings_distribution": ratings_matrix,
        "reviews": reviews_list
    }

    product_query = vendor.products.filter(is_available=True).select_related('sub_category', 'brand')

    # -------------------------------------------------------------------------
    # BRANCH A: E-COMMERCE RETAIL LOGIC
    # -------------------------------------------------------------------------
    if vendor.business_type == 'e_commerce':
        storefront_config, _ = StorefrontConfig.objects.get_or_create(profile=vendor)
        
        if filters.subcategory:
            product_query = product_query.filter(sub_category__name__iexact=filters.subcategory)

        products_list = []
        for product in product_query:
            products_list.append({
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "price": float(product.price),
                "image": product.image.url if product.image else None,
                "current_price": float(product.current_price),
                "bonanza_mode": product.bonanza_mode,
                "discount_percentage": product.discount_percentage,
                "brand_name": product.brand.name if product.brand else None,
                "sub_category_name": product.sub_category.name if product.sub_category else None
            })

        categories_sidebar = []
        sidebar_query = vendor.products.filter(is_available=True)\
                                       .values('sub_category__id', 'sub_category__name')\
                                       .distinct()
        for cat in sidebar_query:
            if cat['sub_category__id']:
                categories_sidebar.append({
                    "id": cat['sub_category__id'],
                    "name": cat['sub_category__name']
                })

        base_data["storefront_meta"] = {
            "hero_image": storefront_config.hero_image.url if storefront_config.hero_image else None,
            "store_slug": storefront_config.store_slug,
            "tagline": storefront_config.tagline,
            "brand_bio": storefront_config.brand_bio,
            "instagram_handle": storefront_config.instagram_handle,
            "twitter_handle": storefront_config.twitter_handle,
        }
        base_data["sidebar_categories"] = categories_sidebar
        base_data["products"] = products_list

    # -------------------------------------------------------------------------
    # BRANCH B: NESTED RESTAURANT MENU ENGINE LOGIC
    # -------------------------------------------------------------------------
    else:
        if filters.subcategory:
            product_query = product_query.filter(sub_category__name__iexact=filters.subcategory)

        # FIX: Use Prefetch object to filter available options in a single query
        available_options_prefetch = Prefetch(
            'menu_options',
            queryset=MenuItemOption.objects.filter(is_available=True),
            to_attr='filtered_menu_options'  # stores results in a Python list attribute
        )
        product_query = product_query.prefetch_related(available_options_prefetch)

        categories_dict = {}
        for product in product_query:
            category_name = "General Items"
            if product.sub_category and product.sub_category.name:
                category_name = product.sub_category.name
            elif hasattr(product, 'category') and product.category:
                category_name = str(product.category)
            
            if category_name not in categories_dict:
                categories_dict[category_name] = []

            # Gather restaurant custom pricing options from the prefetched list (No extra DB hits!)
            menu_options_list = []
            for option in product.filtered_menu_options:
                menu_options_list.append({
                    "id": option.id,
                    "name": option.name,
                    "price": float(option.price),
                    "is_available": option.is_available
                })
                
            categories_dict[category_name].append({
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "price": float(product.price),
                "image": product.image.url if product.image else None,
                "current_price": float(product.current_price),
                "bonanza_mode": product.bonanza_mode,
                "discount_percentage": product.discount_percentage,
                "menu_options": menu_options_list
            })

        base_data["menu_categories"] = categories_dict

    return {
        "status": "success",
        "message": f"Successfully loaded {vendor.company_name}'s store details and reviews.",
        "data": base_data
    }

@router.get("/recommended-vendors")
def get_recommended_vendors(request, category: Optional[str] = None):
    """
    Retrieves recommended storefronts prioritizing active vendors on ₦20k+ subscription tiers,
    high star ratings, and fulfillment reliability.
    """
    now = timezone.now()
    
    # Base query: Approved, active stores
    query = MerchantProfile.objects.filter(
        is_approved=True,
        is_active_store=True
    ).select_related('user', 'user__base_location', 'vendorsubscription', 'vendorsubscription__plan')

    # Filter by category if requested (e.g., 'restaurants' or 'e_commerce')
    if category == 'restaurants':
        query = query.filter(business_type='restaurant')
    elif category == 'e_commerce':
        query = query.filter(business_type='e_commerce')

    recommended_list = []
    
    # Priority sorting: 
    # 1. Active subscription where plan price >= ₦20,000
    # 2. Highest rating score
    # 3. Highest review count
    for vendor in query.order_by(
        '-vendorsubscription__is_active', 
        '-vendorsubscription__plan__price', 
        '-rating', 
        '-review_count'
    )[:10]:
        
        # Check if subscription is valid and >= ₦20,000
        sub = getattr(vendor, 'vendorsubscription', None)
        is_premium_tier = False
        if sub and sub.is_active and sub.subscription_expires_at and sub.subscription_expires_at > now:
            if sub.plan and sub.plan.price >= 20000:
                is_premium_tier = True

        # Optional: Skip stores below a certain threshold if you want strictly high-tier recommendations
        # if not is_premium_tier and vendor.rating < 4.0:
        #     continue

        recommended_list.append({
            "vendor_id": vendor.id,
            "company_name": vendor.company_name,
            "business_type": vendor.business_type,
            "base_location": (
                str(vendor.user.base_location.name)
                if (vendor.user and vendor.user.base_location) else ''
            ),
            "rating": float(vendor.rating) if vendor.rating else 0.0,
            "review_count": vendor.review_count,
            "is_premium_featured": is_premium_tier,
            "is_currently_open": getattr(vendor, 'is_currently_open', True)
        })

    return {
        "status": "success",
        "count": len(recommended_list),
        "data": recommended_list
    }

# =========================================================================
# 3. PRODUCT ATTRIBUTE DETAILS (VARIANTS, SKUS, GALLERY)
# =========================================================================
@router.get("/product/{product_id}", response={200: PublicProductPayloadSchema})
def get_public_product_detail_api(request, product_id: str):
    """
    Asynchronously retrieves comprehensive inventory parameters, media assets, 
    and full variation matrices using a thread pool worker for high concurrency.
    """
    try:
        # Offload compilation onto an isolated read-only worker thread execution lane
        product_data = _sync_get_public_product_worker(
            product_id, request
        )
        
        return 200, {
            "status": "success",
            "message": f"Successfully published details for {product_data['name']}.",
            "data": product_data
        }
        
    except Product.DoesNotExist:
        raise HttpError(404, "Product is either out of stock or does not exist.")
    except Exception as e:
        logger.exception(f"Catalog retrieval crashed for product ID: {product_id}")
        raise HttpError(500, "Unable to load product catalog context.")

def _sync_get_public_product_worker(product_id, request):
    with transaction.atomic():
        # 1. Fetch root product model with related items pre-fetched instantly
        product = Product.objects.select_related('vendor', 'sub_category', 'brand').get(
            id=product_id, 
            is_available=True
        )
        shop = product.vendor
        
        base_host = f"{request.scheme}://{request.get_host()}"
        primary_image_url = f"{base_host}{product.image.url}" if product.image else None

        # 2. Compile Swatch trees without N+1 hits by prefetching related data values
        options_list = []
        options = VariantOption.objects.filter(product=product).prefetch_related('values')
        for option in options:
            options_list.append({
                "option_id": option.id,
                "name": option.name,
                "values": [{"value_id": v.id, "value_text": v.value} for v in option.values.all()]
            })

        # 3. Compile SKUs Pricing & Stock Matrix efficiently
        sku_matrix = []
        active_items = ProductItem.objects.filter(product=product, is_active=True).prefetch_related('variant_values')
        
        for item in active_items:
            # Safely fallback or calculate promotional bonanza updates
            d_price = item.discount_price if item.bonanza_mode and item.discount_price else None
            if not item.bonanza_mode and product.bonanza_mode:
                d_price = item.current_price

            sku_matrix.append({
                "id": item.id,
                "sku": getattr(item, 'sku', f"{product.id}-SKU-{item.id}"),
                "base_price": item.base_selling_price,
                "current_price": item.current_price,
                "discount_price": d_price,
                "bonanza_mode": item.bonanza_mode or product.bonanza_mode,
                "discount_percentage": int(item.discount_percentage),
                "stock_qty": item.stock_qty,
                "value_ids": [v.id for v in item.variant_values.all()]
            })

        # 4. Pull Media Gallery Assets with absolute host pathing fixed!
        gallery_images = []
        images = ProductImage.objects.filter(product=product)
        for img in images:
            gallery_images.append({
                "id": img.id,
                "image": f"{base_host}{img.image.url}" if img.image else "",
                "caption": img.caption or ""
            })

        # Assemble unified primitive result dictionary package
        return {
            "id": product.id,
            "name": product.name,
            "description": product.description or "",
            "key_features": product.key_features if isinstance(product.key_features, list) else [],
            "base_price": product.price,
            "current_price": product.current_price,
            "category": product.category,
            "sub_category": product.sub_category.name if product.sub_category else None,
            "brand": product.brand.name if product.brand else None,
            "bonanza_mode": product.bonanza_mode,
            "discount_percentage": int(product.discount_percentage),
            "image": primary_image_url,
            "is_available": product.is_available,
            "vendor": {
                "id": shop.id,
                "company_name": shop.company_name,
                "business_type": shop.business_type,
                "is_currently_open": getattr(shop, 'is_currently_open', True)
            },
            "gallery_images": gallery_images,
            "variant_options": options_list,
            "sku_matrix": sku_matrix
        }

# =========================================================================
# 4/5. UTILS: SUB-CATEGORIES AND BRANDS
# =========================================================================
@router.get("/utils/subcategories", auth=JWTAuth())
def load_sub_categories_api(request, filters: CategoryFilterSchema = Query(...)):
    """
    Returns subcategories filtered dynamically by the calling merchant's 
    niche industry department domain (e.g., Gadgets, Apparel, etc.).
    """
    resolved_category = None

    # If the client explicitly passes a filter parameter, respect it
    if filters.category and filters.category.strip() != "":
        resolved_category = filters.category.strip().lower()
    else:
        # Look up the merchant's active vertical on their profile
        try:
            profile = MerchantProfile.objects.get(user=request.user)
            if str(profile.business_type).lower().strip() == 'restaurant':
                resolved_category = 'meals'
            else:
                # This automatically resolves to 'gadgets', 'apparel', etc. based on their onboarding registry
                resolved_category = str(profile.ecommerce_sub_category).lower().strip() if profile.ecommerce_sub_category else 'general'
        except MerchantProfile.DoesNotExist:
            resolved_category = 'general'

    # 3. Pull all matching options mapping to that specific retail domain vertical
    if resolved_category and resolved_category != 'all':
        queryset = SubCategory.objects.filter(primary_category=resolved_category).values('id', 'name')
    else:
        queryset = SubCategory.objects.all().values('id', 'name')
        
    sub_categories = []
    for sub in queryset:
        sub_categories.append({"id": sub['id'], "name": sub['name']})
        
    return {"status": "success", "sub_categories": sub_categories}

@router.get("/utils/brands", auth=JWTAuth())
def load_brands_api(request, filters: SubCategoryFilterSchema = Query(...)):
    """
    Returns global brands combined with merchant custom profiles.
    Falls back dynamically to the merchant's entire industry category domain 
    (e.g., 'gadgets') if no subcategory is selected yet.
    """
    user = request.user
    
    # Establish core multi-tenant ownership filters
    brand_filter = Q(created_by__isnull=True)
    if user and user.is_authenticated:
        brand_filter |= Q(created_by=user)
        
    brands_query = Brand.objects.filter(brand_filter)
    
    sub_cat_id = str(filters.sub_category_id).strip() if filters.sub_category_id else None
    
    # A subcategory has been selected
    if sub_cat_id and sub_cat_id.upper() != "NONE" and sub_cat_id != "":
        try:
            if sub_cat_id.isdigit():
                sub_category = SubCategory.objects.get(id=int(sub_cat_id))
            else:
                sub_category = SubCategory.objects.get(id=sub_cat_id)
                
            # Filter brands linked to this subcategory OR sharing the parent primary department
            brands_query = brands_query.filter(
                Q(sub_categories__id=sub_category.id) | 
                Q(sub_categories__primary_category=sub_category.primary_category)
            )
            
        except SubCategory.DoesNotExist:
            if sub_cat_id.isdigit():
                brands_query = brands_query.filter(sub_categories__id=int(sub_cat_id))
            else:
                brands_query = brands_query.filter(sub_categories__id=sub_cat_id)
                
    # No subcategory selected yet -> Fallback to the merchant's entire vertical
    else:
        try:
            profile = MerchantProfile.objects.get(user=request.user)
            if str(profile.business_type).lower().strip() != 'restaurant':
                resolved_category = str(profile.ecommerce_sub_category).lower().strip() if profile.ecommerce_sub_category else None
                
                if resolved_category:
                    # Automatically fetch all pre-available brands that belong to any subcategory under this department
                    brands_query = brands_query.filter(sub_categories__primary_category=resolved_category)
        except MerchantProfile.DoesNotExist:
            pass # Fall back to showing unrestricted brands if no profile matches

    # 4. Serialize out unique records
    brands_query = brands_query.distinct()
    
    brands_list = []
    for b in brands_query.values('id', 'name'):
        brands_list.append({"id": b['id'], "name": b['name']})
        
    return {"status": "success", "brands": brands_list}

# =========================================================================
# DATABASE THREAD CONNECTION OPERATIONS CORES
# =========================================================================
def _cart_matches_order(order, clean_quantity_map) -> bool:
    """True if an existing PENDING order contains exactly the requested cart."""
    items = list(order.items.all())
    if len(items) != len(clean_quantity_map):
        return False
    remaining = dict(clean_quantity_map)
    for item in items:
        for key in (str(item.product_item_id), str(item.product_id)):
            if remaining.get(key) == item.quantity:
                del remaining[key]
                break
        else:
            return False
    return not remaining


def _sync_atomic_order_writer(customer_profile, vendor, input_data):
    """
    Creates the order atomically: validates the cart, locks and decrements stock
    with conditional F() updates (no oversell under concurrency), prices every
    line through the model's current_price rules, applies wallet credit and
    returns the (possibly pre-existing, identical) PENDING order.
    """
    if hasattr(vendor, 'is_currently_open') and not vendor.is_currently_open:
        raise ValueError(f"We are sorry, {vendor.company_name} is currently closed.")

    is_restaurant = str(getattr(vendor, 'business_type', '')).lower() == 'restaurant'

    if len(input_data.items) > MAX_CART_LINES:
        raise ValueError("Too many different items in one order.")

    clean_quantity_map = {}
    for item in input_data.items:
        if item.product_id:
            if item.quantity < 1 or item.quantity > MAX_LINE_QUANTITY:
                raise ValueError(f"Quantity must be between 1 and {MAX_LINE_QUANTITY}.")
            raw_id = str(item.product_id).strip()
            clean_quantity_map[raw_id] = clean_quantity_map.get(raw_id, 0) + item.quantity
    if not clean_quantity_map:
        raise ValueError("Cart is empty.")
    if any(q > MAX_LINE_QUANTITY for q in clean_quantity_map.values()):
        raise ValueError(f"Quantity must be between 1 and {MAX_LINE_QUANTITY}.")

    clean_base_loc = (input_data.base_location or "").strip()[:50]
    clean_delivery_addr = (input_data.delivery_address or "").strip()
    if not clean_delivery_addr:
        raise ValueError("A delivery address is required.")

    # Who delivers? (platform riders vs. the vendor) - may raise a customer-facing ValueError.
    order_fulfillment_mode, self_delivery_fee = decide_fulfillment(
        vendor, customer_profile, input_data.base_location
    )
    is_vendor_delivery = (
        order_fulfillment_mode == MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY
    )

    with transaction.atomic():
        # Serialise concurrent checkouts of the same customer (double-click, retries).
        IndividualProfile.objects.select_for_update().get(pk=customer_profile.pk)

        # --- IDEMPOTENCY / DOUBLE-CLICK GUARD: reuse only an IDENTICAL cart ---
        recent = (
            MarketplaceOrder.objects.filter(
                customer=customer_profile,
                vendor=vendor,
                status=MarketplaceOrder.OrderStatus.PENDING,
                created_at__gte=timezone.now() - timedelta(seconds=15),
            )
            .prefetch_related('items')
            .order_by('-created_at')
        )
        for existing_order in recent:
            if _cart_matches_order(existing_order, clean_quantity_map):
                return existing_order

        marketplace_order = MarketplaceOrder.objects.create(
            customer=customer_profile,
            vendor=vendor,
            delivery_address=clean_delivery_addr,
            base_location=clean_base_loc,
            status=MarketplaceOrder.OrderStatus.PENDING,
            fulfillment_mode=order_fulfillment_mode,
        )

        order_items_to_create = []
        running_items_subtotal = Decimal("0.00")

        if is_restaurant:
            products = {
                str(p.id): p
                for p in Product.objects.filter(
                    id__in=clean_quantity_map.keys(), vendor=vendor, is_available=True
                )
            }
            missing = [pid for pid in clean_quantity_map if pid not in products]
            if missing:
                raise ValueError("Some items in your cart are no longer available at this restaurant.")
            for pid, qty in clean_quantity_map.items():
                product = products[pid]
                price = Decimal(str(product.current_price))
                running_items_subtotal += price * qty
                order_items_to_create.append(MarketplaceOrderItem(
                    order=marketplace_order, product_item=None, product=product,
                    quantity=qty, price_at_order=price,
                ))
        else:
            sku_keys = [k for k in clean_quantity_map if k.isdigit()]
            product_keys = [k for k in clean_quantity_map if not k.isdigit()]
            if any(len(k) > 12 for k in sku_keys):  # would overflow the integer id column
                raise ValueError("A selected item is no longer available at this storefront.")

            # Lock every candidate SKU row (ordered by id to avoid deadlocks).
            candidate_variants = list(
                ProductItem.objects.select_related('product')
                .select_for_update(of=('self',))
                .filter(
                    Q(id__in=[int(k) for k in sku_keys]) | Q(product__id__in=product_keys),
                    product__vendor=vendor,
                    product__is_available=True,
                    is_active=True,
                )
                .order_by('id')
            )
            by_sku = {str(v.id): v for v in candidate_variants if str(v.id) in sku_keys}
            by_product = {}
            for v in candidate_variants:
                if str(v.product_id) in product_keys:
                    by_product.setdefault(str(v.product_id), []).append(v)

            resolved = []  # (variant, qty)
            for key, qty in clean_quantity_map.items():
                if key.isdigit():
                    variant = by_sku.get(key)
                    if not variant:
                        raise ValueError("A selected item is no longer available at this storefront.")
                else:
                    options = by_product.get(key, [])
                    if len(options) == 1:
                        variant = options[0]
                    elif len(options) > 1:
                        # A parent id with several variants is ambiguous: never multiply the
                        # quantity across every variant.
                        raise ValueError(f"Please choose a variant for {options[0].product.name}.")
                    else:
                        product = Product.objects.filter(id=key, vendor=vendor, is_available=True).first()
                        if not product:
                            raise ValueError("A selected item is no longer available at this storefront.")
                        if ProductItem.objects.filter(product=product).exists():
                            raise ValueError(f"{product.name} is currently unavailable.")
                        # Product without any SKU row: provision its default SKU.
                        variant = ProductItem.objects.create(product=product, stock_qty=1000, is_active=True)
                        variant.product = product
                resolved.append((variant, qty))

            for variant, qty in resolved:
                # Conditional decrement: succeeds only while enough stock remains.
                decremented = ProductItem.objects.filter(
                    pk=variant.pk, stock_qty__gte=qty
                ).update(stock_qty=F('stock_qty') - qty)
                if not decremented:
                    raise ValueError(f"Insufficient stock for {variant.product.name}.")

                price = Decimal(str(variant.current_price))
                running_items_subtotal += price * qty
                order_items_to_create.append(MarketplaceOrderItem(
                    order=marketplace_order, product_item=variant, product=variant.product,
                    quantity=qty, price_at_order=price,
                ))

        if not order_items_to_create:
            raise ValueError("Selected items are unavailable at this storefront.")

        MarketplaceOrderItem.objects.bulk_create(order_items_to_create)

        if is_vendor_delivery:
            # The vendor delivers and sets its own charge (0 = free delivery).
            delivery_charge = Decimal(self_delivery_fee or 0)
        else:
            vendor_user = getattr(vendor, 'user', None)
            vendor_lat = getattr(vendor, 'latitude', None) or getattr(vendor_user, 'latitude', None)
            vendor_lng = getattr(vendor, 'longitude', None) or getattr(vendor_user, 'longitude', None)
            customer_lat = input_data.latitude or getattr(customer_profile, 'latitude', None)
            customer_lng = input_data.longitude or getattr(customer_profile, 'longitude', None)
            # Never trust device coordinates blindly: they must fall inside the chosen delivery area.
            customer_lat, customer_lng = sanitize_customer_coordinates(
                customer_lat, customer_lng,
                _resolve_buyer_area(input_data.base_location, customer_profile),
            )
            delivery_charge = _calculate_distance_delivery_fee(vendor_lat, vendor_lng, customer_lat, customer_lng)

        total = running_items_subtotal + delivery_charge
        if total > MAX_ORDER_VALUE:
            raise ValueError("Order value is too large.")

        marketplace_order.subtotal = running_items_subtotal
        marketplace_order.delivery_fee = delivery_charge
        marketplace_order.total_price = total
        marketplace_order.save(update_fields=['subtotal', 'delivery_fee', 'total_price'])

        # Wallet credit offsets the delivery fee; the applied amount is stored on the order.
        apply_wallet_to_order(marketplace_order)

        return marketplace_order


@router.post("/checkout/{vendor_id}", auth=JWTAuth())
async def checkout_order_api(request, vendor_id: str, data: CheckoutRequestInput):
    user = request.user

    if user.role != CustomUser.Role.INDIVIDUAL:
        raise HttpError(403, "Only customer accounts can place orders.")

    try:
        customer_profile = await IndividualProfile.objects.aget(user=user)
    except IndividualProfile.DoesNotExist:
        raise HttpError(400, "Complete your onboarding customer profile setup first.")

    # Exact match only: no partial-ID fallback
    vendor = await MerchantProfile.objects.select_related(
        'user', 'user__base_location', 'shipping_preference'
    ).filter(
        id__iexact=vendor_id.strip(), is_approved=True
    ).afirst()
    if not vendor:
        raise HttpError(404, "Vendor storefront profile not found or not approved.")
    if not vendor.is_currently_open:
        raise HttpError(400, f"{vendor.company_name} is closed.")

    # 1. Create the order (stock reserved, wallet applied)
    try:
        order = await sync_to_async(_sync_atomic_order_writer)(customer_profile, vendor, data)
    except ValueError as e:  # business-rule failures: stock, closed, empty cart
        raise HttpError(400, str(e))
    except Exception:
        logger.exception("Checkout order creation failed")
        raise HttpError(500, "Could not create your order. Please try again.")

    # 2. Create (or reuse) the Paystack link exactly once per order
    try:
        payment = await sync_to_async(ensure_payment_link)(order.id, user.email)
    except Exception:
        logger.exception("Paystack init failed for order %s", order.id)
        await sync_to_async(release_unpaid_order)(order.id)  # give back stock + wallet credit
        raise HttpError(502, "Payment service unavailable. Please try again.")

    await order.arefresh_from_db(fields=['total_price'])

    return {
        "status": "success",
        "checkout_url": payment["checkout_url"],
        "reference": payment["reference"],
        "amount": float(order.total_price),
        "email": user.email,
        "name": f"{user.first_name} {user.last_name}".strip() or "Customer",
        "phone": user.phone_number or "",
        "order_id": str(order.id),
    }


# =========================================================================
# PAYMENT VERIFICATION
# =========================================================================
# Authenticated JSON endpoint for the web client
@router.get("/verify-web/{order_id}", auth=JWTAuth())
async def verify_order_payment_web_json_api(request, order_id: str):
    try:
        order_obj = await MarketplaceOrder.objects.select_related(
            'customer', 'customer__user', 'vendor'
        ).aget(id=order_id, customer__user=request.user)  # ownership check
    except MarketplaceOrder.DoesNotExist:
        raise HttpError(404, "Order not found.")

    if not await _confirm_and_finalize(order_obj):
        raise HttpError(400, "Payment not confirmed yet.")

    return {"status": "success", "message": "Payment verified successfully.", "order_id": order_id}


# Browser redirect target for Paystack / the mobile deep link. Paystack's redirect
# cannot send a JWT, so there is no auth here. It is safe by construction: nothing
# from the request is trusted except the order id, and payment is only accepted
# when Paystack confirms the reference stored on that order, its amount and currency.
@router.get("/order/verify")
async def verify_order_payment_get_api(request, order_id: str):
    try:
        order_obj = await MarketplaceOrder.objects.select_related(
            'customer', 'customer__user', 'vendor'
        ).aget(id=order_id)
    except MarketplaceOrder.DoesNotExist:
        raise HttpError(404, "Order not found.")

    if not await _confirm_and_finalize(order_obj):
        raise HttpError(404, "Payment not confirmed")

    response = HttpResponse(status=302)
    response['Location'] = f"aikacustomer://payment-success?order_id={order_obj.id}"
    return response


# =========================================================================
# ORDER SUMMARY, QUOTES, RECEIPT
# =========================================================================
@router.get("/order-summary/{order_id}", auth=JWTAuth(), response=OrderSummaryResponse)
def order_summary_api(request, order_id: str):
    """
    Returns flat, high-performance validated asset structural data arrays to client screens,
    enforcing strict ownership and role-based access control (RBAC).
    """
    user = request.auth
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")

    try:
        # Optimize query by selecting related vendor and customer/user profiles
        order = MarketplaceOrder.objects.select_related(
            'vendor', 
            'vendor__user', 
            'customer', 
            'customer__user'
        ).get(id=order_id)
    except MarketplaceOrder.DoesNotExist:
        raise HttpError(404, "Order transaction instance missing.")

    # =========================================================================
    # AUTHORIZATION BOUNDARY CHECK (IDOR Prevention)
    # =========================================================================
    is_management = (
        user.is_superuser or 
        user.is_staff or 
        getattr(user, 'role', None) in ['admin', 'staff', 'agent']
    )

    if not is_management:
        is_authorized_customer = False
        is_authorized_vendor = False

        # 1. Check if the user is the customer who placed the order
        if hasattr(order, 'customer') and order.customer:
            if order.customer == user or getattr(order.customer, 'user_id', None) == user.id:
                is_authorized_customer = True

        # 2. Check if the user is the vendor fulfilling the order
        if hasattr(order, 'vendor') and order.vendor:
            if getattr(order.vendor, 'user_id', None) == user.id:
                is_authorized_vendor = True

        # Deny access if neither owner nor management
        if not (is_authorized_customer or is_authorized_vendor):
            raise HttpError(403, "Access denied. You do not have permission to view this order.")
    # =========================================================================

    # Join across the SKU table down to the parent product properties smoothly
    items_cursor = MarketplaceOrderItem.objects.filter(order=order).select_related(
        'product_item__product', 
        'product'
    )
    serialized_items = []
    
    for item in items_cursor:
        if item.product_item:
            p_id = item.product_item.product.id
            p_name = item.product_item.product.name
        elif item.product:
            p_id = item.product.id
            p_name = item.product.name
        else:
            p_id = "UNKNOWN"
            p_name = "Unknown Item"

        serialized_items.append({
            "product_id": str(p_id),
            "product_name": str(p_name),
            "quantity": int(item.quantity),
            "price_at_order": float(item.price_at_order)
        })

    full_delivery_location = f"{order.delivery_address}, {order.base_location}"

    return {
        "status": "success",
        "order_id": order.id,
        "vendor_name": order.vendor.company_name,
        "delivery_address": full_delivery_location,
        "item_total": float(order.subtotal),
        "delivery_charge": float(order.delivery_fee),
        "total_price": float(order.total_price),
        "order_status": str(order.status),
        "items": serialized_items
    }

@router.post("/quote-delivery/{vendor_id}", auth=JWTAuth())
def quote_delivery_fee_api(request, vendor_id: str, data: QuoteRequestInput):
    """
    Predicts the delivery option and fee checkout will apply: platform riders (distance fee,
    wallet credit applied) or the vendor's own delivery (the vendor's flat charge, no wallet).
    """
    user = request.user
    clean_vendor_id = vendor_id.strip()

    vendor = MerchantProfile.objects.select_related(
        'user', 'user__base_location', 'shipping_preference'
    ).filter(
        id__iexact=clean_vendor_id,
        is_approved=True
    ).first()

    if not vendor:
        raise HttpError(404, "Vendor storefront profile not found or not approved.")

    customer_profile = IndividualProfile.objects.select_related('user', 'user__base_location').filter(user=user).first()

    try:
        mode, vendor_fee = decide_fulfillment(vendor, customer_profile, data.base_location)
    except AreaRequiredError as exc:
        # The UI asks the customer to pick an area, then asks for the quote again.
        return {
            "status": "needs_area",
            "message": str(exc),
            "fulfillment_mode": None,
            "delivered_by": None,
            "delivery_fee": 0.0,
            "wallet_balance": 0.0,
            "wallet_applied_to_delivery": 0.0,
            "net_delivery_fee": 0.0,
        }
    except ValueError as exc:
        raise HttpError(400, str(exc))

    if mode == MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY:
        fee = Decimal(vendor_fee or 0)
        return {
            "status": "success",
            "fulfillment_mode": mode,
            "delivered_by": "vendor",
            "message": f"{vendor.company_name} will deliver this order itself.",
            "delivery_fee": float(fee),
            "wallet_balance": 0.0,
            "wallet_applied_to_delivery": 0.0,
            "net_delivery_fee": float(fee),
        }

    wallet_balance = Decimal('0.00')
    if user and not user.is_anonymous:
        try:
            wallet = CustomerWallet.objects.get(user=user, is_active=True)
            wallet_balance = wallet.balance
        except CustomerWallet.DoesNotExist:
            pass

    vendor_lat = getattr(vendor, 'latitude', None) or (getattr(vendor, 'user', None) and getattr(vendor.user, 'latitude', None)) or ''
    vendor_lng = getattr(vendor, 'longitude', None) or (getattr(vendor, 'user', None) and getattr(vendor.user, 'longitude', None)) or ''

    quote_lat, quote_lng = sanitize_customer_coordinates(
        data.latitude, data.longitude, _resolve_buyer_area(data.base_location, customer_profile)
    )
    delivery_fee = _calculate_distance_delivery_fee(
        vendor_lat, vendor_lng, quote_lat or '', quote_lng or ''
    )

    wallet_applied = min(delivery_fee, wallet_balance)
    net_delivery_fee = delivery_fee - wallet_applied

    return {
        "status": "success",
        "fulfillment_mode": mode,
        "delivered_by": "platform",
        "message": "",
        "delivery_fee": float(delivery_fee),
        "wallet_balance": float(wallet_balance),
        "wallet_applied_to_delivery": float(wallet_applied),
        "net_delivery_fee": float(net_delivery_fee)
    }


@router.get("/receipt/{order_id}", auth=JWTAuth())
def download_receipt(request, order_id: str):
    user = request.user
    try:
        order = (
            MarketplaceOrder.objects
            .select_related('vendor', 'vendor__user', 'customer', 'customer__user')
            .prefetch_related('items__product', 'items__product_item__product')
            .get(id=order_id)
        )
    except MarketplaceOrder.DoesNotExist:
        raise HttpError(404, "Order not found.")

    # Ownership / management check (IDOR protection)
    is_management = user.is_superuser or user.is_staff or getattr(user, 'role', None) in ('admin', 'staff', 'agent')
    is_customer = bool(order.customer and order.customer.user_id == user.id)
    is_vendor = bool(order.vendor and order.vendor.user_id == user.id)
    if not (is_management or is_customer or is_vendor):
        raise HttpError(403, "Access denied. You do not have permission to view this receipt.")

    # A receipt proves payment: never issue one for an unpaid or cancelled order.
    if order.status not in PAID_ORDER_STATES:
        raise HttpError(400, "A receipt is only available for paid orders.")

    html_string = render_to_string('receipt_template.html', {'order': order})

    def _block_external_fetch(url, *args, **kwargs):
        # The receipt is fully self-contained; refuse every network/file fetch.
        raise ValueError("External resources are disabled for receipts.")

    pdf_file = HTML(string=html_string, url_fetcher=_block_external_fetch).write_pdf()

    safe_ref = "".join(c for c in str(order.id) if c.isalnum() or c in "-_")
    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="receipt_{safe_ref}.pdf"'
    return response


@router.get("/vendor/{vendor_id}/coordinates")
async def get_vendor_coordinates_api(request, vendor_id: str):
    """
    Retrieves the operational latitude, longitude, and base location 
    for a specific approved merchant storefront.
    """
    try:
        vendor = await MerchantProfile.objects.select_related(
            "user", "user__base_location"
        ).aget(id=vendor_id, is_approved=True)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Vendor not found.")

    lat = getattr(vendor, "latitude", None)
    if lat is None and vendor.user:
        lat = getattr(vendor.user, "latitude", None)
    if lat is None and vendor.user and vendor.user.base_location:
        lat = vendor.user.base_location.latitude

    lng = getattr(vendor, "longitude", None)
    if lng is None and vendor.user:
        lng = getattr(vendor.user, "longitude", None)
    if lng is None and vendor.user and vendor.user.base_location:
        lng = vendor.user.base_location.longitude

    base = ""
    if getattr(vendor, "base_location", None):
        base = str(getattr(vendor.base_location, "name", vendor.base_location))
    elif vendor.user and vendor.user.base_location:
        base = str(vendor.user.base_location.name)

    address = getattr(vendor, "street_address", None) or (
        getattr(vendor.user, "street_address", None) if vendor.user else None
    )

    return {
        "status": "success",
        "data": {
            "vendor_id": str(vendor.id),
            "company_name": vendor.company_name,
            "latitude": float(lat) if lat is not None else None,
            "longitude": float(lng) if lng is not None else None,
            "base_location": base,
            "street_address": address,
        },
    }

@router.post("/quote-delivery/{vendor_id}", auth=JWTAuth())
def quote_delivery_fee_api(request, vendor_id: str, data: QuoteRequestInput):
    """
    Synchronously calculates and returns the precise distance-based delivery fee 
    and available wallet coverage for the authenticated customer.
    """
    user = request.user
    clean_vendor_id = vendor_id.strip()

    # 1. Vendor Lookup
    vendor = MerchantProfile.objects.select_related('user').filter(
        id__iexact=clean_vendor_id,
        is_approved=True
    ).first()

    if not vendor:
        raise HttpError(404, "Vendor storefront profile not found or not approved.")

    # 2. Customer Wallet Balance Lookup
    wallet_balance = Decimal('0.00')
    if user and not user.is_anonymous:
        try:
            wallet = CustomerWallet.objects.get(user=user, is_active=True)
            wallet_balance = wallet.balance
        except CustomerWallet.DoesNotExist:
            pass

    # 3. Extract Coordinates & Calculate Distance Fee Directly
    vendor_lat = getattr(vendor, 'latitude', None) or (getattr(vendor, 'user', None) and getattr(vendor.user, 'latitude', None)) or ''
    vendor_lng = getattr(vendor, 'longitude', None) or (getattr(vendor, 'user', None) and getattr(vendor.user, 'longitude', None)) or ''
    
    customer_lat = data.latitude or ''
    customer_lng = data.longitude or ''

    delivery_fee = _calculate_distance_delivery_fee(
        vendor_lat, vendor_lng, customer_lat, customer_lng
    )

    wallet_applied = min(delivery_fee, wallet_balance)
    net_delivery_fee = delivery_fee - wallet_applied

    return {
        "status": "success",
        "delivery_fee": float(delivery_fee),
        "wallet_balance": float(wallet_balance),
        "wallet_applied_to_delivery": float(wallet_applied),
        "net_delivery_fee": float(net_delivery_fee)
    }
