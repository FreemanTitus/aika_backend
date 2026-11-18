from django.conf import settings
import logging
from django.core.cache import cache
from django.core.exceptions import ValidationError
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
from marketplace.utils import base_delivery_fare


logger = logging.getLogger(__name__)

router = Router()

# =========================================================================
# PUBLIC HOME FEED (landing page: featured meals + popular restaurants)
# =========================================================================
HOME_FEED_CACHE_KEY = "marketplace_home_feed_v1"
HOME_FEED_TTL_SECONDS = 60
FOOD_CATEGORIES = ('meals', 'burgers', 'drinks')


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


HOME_FEED_CACHE_KEY = "marketplace_home_feed_v1"
HOME_FEED_TTL_SECONDS = 60
FOOD_CATEGORIES = ('meals', 'burgers', 'drinks')

def _build_home_feed():
    # 1. Pull featured restaurants, optimizing with select_related for storefront_config
    restaurants_qs = list(
        MerchantProfile.objects.filter(
            is_approved=True, 
            is_active_store=True, 
            business_type='restaurant',
            vendorsubscription__plan__has_featured_placement=True,
            vendorsubscription__is_active=True
        ).select_related('storefront_config').order_by('-rating', '-review_count')[:12]
    )
    
    vendor_ids = [r.id for r in restaurants_qs]
    label_for = dict(Product.CategoryChoices.choices)

    # Fallback if no vendors match the featured tier yet
    if not vendor_ids:
        restaurants_qs = list(
            MerchantProfile.objects.filter(
                is_approved=True, is_active_store=True, business_type='restaurant'
            ).select_related('storefront_config').order_by('-rating', '-review_count')[:12]
        )
        vendor_ids = [r.id for r in restaurants_qs]

    # 2. Fetch dishes for the featured meals section
    dishes = list(
        Product.objects.filter(vendor_id__in=vendor_ids, is_available=True, category__in=FOOD_CATEGORIES)
        .exclude(image='').exclude(image__isnull=True)
        .select_related('vendor', 'sub_category')
        .order_by('-bonanza_mode', '-vendor__rating', 'name')[:24]
    )
    
    # 3. Compile categories per vendor
    categories_by_vendor = {}
    vendor_subcategories = (
        Product.objects.filter(vendor_id__in=vendor_ids, is_available=True, category__in=FOOD_CATEGORIES)
        .select_related('sub_category')
        .values_list('vendor_id', 'sub_category__name', 'category')
        .distinct()
    )
    
    for vendor_id, sub_name, primary_cat in vendor_subcategories:
        tag_label = sub_name if sub_name else label_for.get(primary_cat, primary_cat)
        categories_by_vendor.setdefault(vendor_id, []).append(tag_label)

    fare = float(base_delivery_fare())

    def badge(vendor, product=None):
        if product is not None and product.bonanza_mode:
            return "Hot deal"
        return "Top rated" if (vendor.rating or 0) >= 4.5 else "Popular"

    meals = []
    seen_categories = {}
    for product in dishes:
        cat_key = product.sub_category.slug if product.sub_category else product.category
        cat_label = product.sub_category.name if product.sub_category else label_for.get(product.category, product.category)
        
        seen_categories[cat_key] = cat_label
        
        meals.append({
            "id": product.id,
            "name": product.name,
            "price": float(product.current_price),
            "image": product.image.url if product.image else None,
            "category": cat_key,
            "category_label": cat_label,
            "badge": badge(product.vendor, product),
            "vendor_id": product.vendor_id,
            "vendor_name": product.vendor.company_name,
            "vendor_rating": float(product.vendor.rating or 0),
            "prep_minutes": 25,
        })

    restaurants = []
    for vendor in restaurants_qs:
        # Check if the vendor has their own custom cover/hero image via StorefrontConfig, 
        # with a fallback to their top product image if no storefront banner is uploaded.
        custom_cover = None
        if hasattr(vendor, 'storefront_config') and vendor.storefront_config and vendor.storefront_config.hero_image:
            custom_cover = vendor.storefront_config.hero_image.url
        else:
            # Fallback to first available product image for this vendor if no custom cover exists
            first_product = Product.objects.filter(vendor_id=vendor.id, is_available=True).exclude(image='').exclude(image__isnull=True).first()
            custom_cover = first_product.image.url if first_product and first_product.image else None

        restaurants.append({
            "id": vendor.id,
            "name": vendor.company_name,
            "rating": float(vendor.rating or 0),
            "review_count": vendor.review_count,
            "is_open": bool(getattr(vendor, 'is_currently_open', True)),
            "cover_image": custom_cover,  # <-- Uses merchant's own cover image
            "tags": categories_by_vendor.get(vendor.id, [])[:3],
            "prep_minutes": 25,
            "delivery_fee": fare,
            "badge": badge(vendor),
        })

    return {
        "status": "success",
        "featured_meals": meals,
        "meal_categories": [{"key": key, "label": label} for key, label in seen_categories.items()],
        "restaurants": restaurants,
    }


@router.get("/home-feed")
def get_home_feed(request):
    """Public landing-page data. Cached for a minute: it is hit by every visitor."""
    return cache.get_or_set(HOME_FEED_CACHE_KEY, _build_home_feed, HOME_FEED_TTL_SECONDS)