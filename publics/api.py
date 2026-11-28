from django.conf import settings
import logging
from typing import Optional
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Value, DecimalField
from django.db.models.functions import Coalesce
from decimal import Decimal
from django.core.validators import validate_email
from ninja import Router
from ninja.errors import HttpError
from .models import NewsletterSubscriber
from .schemas import NewsletterSubscribeSchema
from customers.models import FrequentlyAskedQuestion
from accounts.models import CustomerReview
from accounts.schemas import CustomerReviewSchema
from merchant.models import MerchantProfile
from marketplace.models import Product
from django.utils import timezone
from marketplace.utils import base_delivery_fare
from merchant.utils import active_merchant_access_q

logger = logging.getLogger(__name__)

router = Router()

# =========================================================================
# PUBLIC HOME FEED (landing page: featured meals + popular restaurants)
# =========================================================================
HOME_FEED_CACHE_PREFIX = "marketplace_home_feed_v1"
HOME_FEED_TTL_SECONDS = 60
FOOD_CATEGORIES = ('meals', 'burgers', 'drinks')


CATEGORY_LABELS = {
    "gadgets": "Gadgets & Tech",
    "apparel": "Clothing & Apparel",
    "appliances": "Home Appliances",
    "beauty": "Beauty & Cosmetics",
    "restaurant": "Restaurant",
    "general": "General Items",
}


def get_product_category_label(product) -> str:
    # Pull display labels from MerchantProfile enums when available
    from merchant.models import MerchantProfile

    labels = dict(CATEGORY_LABELS)
    for choices_attr in ("EcommerceCategory", "BusinessType"):
        enum_cls = getattr(MerchantProfile, choices_attr, None)
        if enum_cls is not None and hasattr(enum_cls, "choices"):
            for key, label in enum_cls.choices:
                labels.setdefault(str(key).lower(), str(label))

    raw_cat = (getattr(product, "category", "") or "").strip().lower()
    if raw_cat in labels:
        return labels[raw_cat]
    if getattr(product, "sub_category", None) and product.sub_category.name:
        return product.sub_category.name
    return raw_cat.replace("_", " ").title() if raw_cat else "General Items"


# Public Subscription Endpoint
@router.post("/newsletter/subscribe", auth=None)
def subscribe_newsletter(request, payload: NewsletterSubscribeSchema):
    email = (payload.email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise HttpError(400, "Please provide a valid email address.")

    subscriber, created = NewsletterSubscriber.objects.get_or_create(
        email=email,
        defaults={'is_active': True}
    )
    
    if not created and not subscriber.is_active:
        subscriber.is_active = True
        subscriber.save(update_fields=['is_active'])

    return {"status": "success", "message": "Successfully subscribed to our newsletter!"}

@router.post("/submit-review", auth=None)  # Publicly accessible for customers/visitors
def submit_customer_review(request, payload: CustomerReviewSchema):
    if not payload.name or not payload.comment:
        raise HttpError(400, "Name and comment are required fields.")
    if len(payload.name) > 100 or len(payload.comment) > 1000 or len(payload.role or "") > 100:
        raise HttpError(400, "Submission is too long.")

    review = CustomerReview.objects.create(
        name=payload.name,
        role=payload.role,
        comment=payload.comment,
        is_approved=False  # Set to True if you want them to appear immediately
    )

    return {
        "status": "success", 
        "message": "Thank you! Your review has been submitted successfully."
    }

@router.get("/reviews", auth=None)
def list_customer_reviews(request):
    # Fetch approved reviews ordered by newest first
    reviews = CustomerReview.objects.filter(is_approved=True).order_by('-created_at')[:10]
    
    return {
        "reviews": [
            {
                "id": r.id,
                "name": r.name,
                "role": r.role,
                "comment": r.comment
            } for r in reviews
        ]
    }

@router.get("/faqs", auth=None)
def list_public_faqs(request):
    faqs = FrequentlyAskedQuestion.objects.filter(is_active=True)
    return {
        "faqs": [
            {
                "id": f.id,
                "question": f.question,
                "answer": f.answer,
                "order": f.order
            } for f in faqs
        ]
    }


@router.get("/vapid-key")
def get_vapid_public_key(request):
    """
    Public VAPID key the browser needs for push subscriptions (so it is not hard-coded in the
    frontend). It must be the raw 65-byte uncompressed P-256 key, base64url encoded.
    """
    import base64

    key = str(getattr(settings, "VAPID_PUBLIC_KEY", "") or "").strip()
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    except Exception:
        raw = b""
    if len(raw) != 65 or raw[0] != 0x04:
        logger.error("VAPID_PUBLIC_KEY is not a raw 65-byte uncompressed P-256 key; push cannot work.")
        raise HttpError(503, "Push notifications are not configured.")
    return {"public_key": key}


def _build_home_feed(customer_location_id: Optional[int] = None):
    now = timezone.now()

    base_restaurants = (
        MerchantProfile.objects.filter(
            is_approved=True,
            is_active_store=True,
            business_type="restaurant",
        )
        .filter(active_merchant_access_q(now))
        .select_related("storefront_config", "user", "user__base_location")
        .annotate(
            effective_plan_price=Coalesce(
                "vendorsubscription__plan__price",
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .distinct()
    )

    if customer_location_id is not None:
        base_restaurants = base_restaurants.filter(user__base_location_id=int(customer_location_id))

    # 1. Pull featured restaurants first (featured plan flag or highest plan tier)
    restaurants_qs = list(
        base_restaurants.filter(
            vendorsubscription__plan__has_featured_placement=True,
            vendorsubscription__is_active=True,
        ).order_by("-effective_plan_price", "-rating", "-review_count")[:12]
    )

    # Fallback to all active restaurants in the hub if none have featured placement yet
    if not restaurants_qs:
        restaurants_qs = list(
            base_restaurants.order_by("-effective_plan_price", "-rating", "-review_count")[:12]
        )

    vendor_ids = [r.id for r in restaurants_qs]

    # 2. Fetch dishes for the featured meals section (restaurant products use category="restaurant" + sub_category)
    dishes = list(
        Product.objects.filter(
            vendor_id__in=vendor_ids,
            is_available=True,
        )
        .exclude(image="")
        .exclude(image__isnull=True)
        .select_related("vendor", "vendor__storefront_config", "sub_category")
        .order_by("-bonanza_mode", "-vendor__rating", "name")[:24]
    )

    # 3. Compile menu subcategory tags per vendor & pre-fetch fallback cover images in bulk
    categories_by_vendor = {}
    fallback_cover_by_vendor = {}

    vendor_products_meta = (
        Product.objects.filter(vendor_id__in=vendor_ids, is_available=True)
        .select_related("sub_category")
        .order_by("-bonanza_mode", "id")
    )

    for prod in vendor_products_meta:
        tag_label = (
            prod.sub_category.name
            if prod.sub_category
            else get_product_category_label(prod)
        )
        if tag_label and tag_label.lower() != "restaurant":
            vendor_tags = categories_by_vendor.setdefault(prod.vendor_id, [])
            if tag_label not in vendor_tags:
                vendor_tags.append(tag_label)

        if prod.vendor_id not in fallback_cover_by_vendor and prod.image:
            fallback_cover_by_vendor[prod.vendor_id] = prod.image.url

    fare = float(base_delivery_fare())

    def badge(vendor, product=None):
        if product is not None and product.bonanza_mode:
            return "Hot deal"
        return "Top rated" if (vendor.rating or 0) >= 4.5 else "Popular"

    meals = []
    seen_categories = {}
    for product in dishes:
        cat_key = product.sub_category.slug if product.sub_category else (product.category or "restaurant")
        cat_label = (
            product.sub_category.name
            if product.sub_category
            else get_product_category_label(product)
        )
        seen_categories[cat_key] = cat_label

        storefront = getattr(product.vendor, "storefront_config", None)
        meals.append({
            "id": product.id,
            "name": product.name,
            "price": float(product.price),
            "current_price": float(product.current_price),
            "bonanza_mode": bool(product.bonanza_mode),
            "image": product.image.url if product.image else None,
            "category": cat_key,
            "category_label": cat_label,
            "badge": badge(product.vendor, product),
            "vendor_id": product.vendor_id,
            "vendor_name": product.vendor.company_name,
            "vendor_slug": storefront.store_slug if storefront else None,
            "vendor_rating": float(product.vendor.rating or 0),
            "prep_minutes": 25,
        })

    restaurants = []
    for vendor in restaurants_qs:
        storefront = getattr(vendor, "storefront_config", None)
        if storefront and getattr(storefront, "hero_image", None):
            custom_cover = storefront.hero_image.url
        else:
            custom_cover = fallback_cover_by_vendor.get(vendor.id)

        restaurants.append({
            "id": vendor.id,
            "name": vendor.company_name,
            "store_slug": storefront.store_slug if storefront else None,
            "rating": float(vendor.rating or 0),
            "review_count": vendor.review_count,
            "is_open": bool(getattr(vendor, "is_currently_open", True)),
            "cover_image": custom_cover,
            "tags": categories_by_vendor.get(vendor.id, [])[:3],
            "prep_minutes": 25,
            "delivery_fee": fare,
            "badge": badge(vendor),
            "location_name": (
                str(vendor.user.base_location.name)
                if (vendor.user and vendor.user.base_location)
                else ""
            ),
        })

    return {
        "status": "success",
        "featured_meals": meals,
        "meal_categories": [{"key": key, "label": label} for key, label in seen_categories.items()],
        "restaurants": restaurants,
    }


@router.get("/home-feed")
def get_home_feed(request, customer_location_id: Optional[int] = None):
    """Public landing-page data. Cached per location for a minute."""
    if not customer_location_id and request.user and request.user.is_authenticated:
        if hasattr(request.user, "base_location_id"):
            customer_location_id = request.user.base_location_id

    cache_key = f"{HOME_FEED_CACHE_PREFIX}:loc_{customer_location_id or 'all'}"
    return cache.get_or_set(
        cache_key,
        lambda: _build_home_feed(customer_location_id=customer_location_id),
        HOME_FEED_TTL_SECONDS,
    )