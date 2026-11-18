from django.conf import settings
import logging
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from ninja import Router
from ninja.errors import HttpError
from .models import NewsletterSubscriber
from .schemas import NewsletterSubscribeSchema
from customers.models import FrequentlyAskedQuestion
from accounts.models import CustomerReview
from accounts.schemas import CustomerReviewSchema

logger = logging.getLogger(__name__)

router = Router()

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
