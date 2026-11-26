import os
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings
from django.utils.text import slugify
from datetime import timedelta
from django.utils import timezone
from .utils import (
    ensure_merchant_trial_on_approval,
    evaluate_merchant_subscription_or_trial,
)


from datetime import timedelta
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from .utils import (
    ensure_merchant_trial_on_approval,
    evaluate_merchant_subscription_or_trial,
)


class MerchantProfile(models.Model):
    """
    Handles profiles for any registered entity (Restaurants, E-commerce vendors, etc.)
    """
    class BusinessType(models.TextChoices):
        RESTAURANT = 'restaurant', 'Restaurant'
        E_COMMERCE = 'e_commerce', 'E-commerce Vendor'
        OTHER = 'other', 'Other Business'

    # Dedicated sub-categories strictly for E-commerce business types
    class EcommerceCategory(models.TextChoices):
        GADGETS = 'gadgets', 'Gadgets & Tech'
        APPAREL = 'apparel', 'Clothing & Apparel'
        HOME_APPLIANCES = 'appliances', 'Home Appliances'
        BEAUTY = 'beauty', 'Beauty & Cosmetics'

    id = models.CharField(primary_key=True, max_length=20, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='merchant_profile'
    )
    company_name = models.CharField(max_length=255, help_text="Business name or Individual full name")
    business_type = models.CharField(
        max_length=30,
        choices=BusinessType.choices,
        blank=True,
        null=True,
        help_text="Leave blank if user is just a regular individual account"
    )
    ecommerce_sub_category = models.CharField(
        max_length=50,
        choices=EcommerceCategory.choices,
        blank=True,
        null=True,
        help_text="If E-commerce is selected, specify the primary operational niche."
    )
    # PAYSTACK SPLIT PAYMENTS
    paystack_subaccount_code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Paystack subaccount code (e.g., SUB_xxxxxxxx) for automated checkout splits."
    )
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_vendors',
        limit_choices_to={'role': 'staff'},
        help_text="The internal operations agent responsible for overseeing notifications and bike dispatches for this vendor."
    )
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0.00), MaxValueValidator(5.00)],
        null=True, blank=True
    )
    review_count = models.PositiveIntegerField(default=0)
    count_5_stars = models.PositiveIntegerField(default=0)
    count_4_stars = models.PositiveIntegerField(default=0)
    count_3_stars = models.PositiveIntegerField(default=0)
    count_2_stars = models.PositiveIntegerField(default=0)
    count_1_stars = models.PositiveIntegerField(default=0)

    is_approved = models.BooleanField(default=False, help_text="Admin approval for corporate/business accounts")
    is_active_store = models.BooleanField(default=True, help_text="If false, the store will not be visible in the marketplace or accept orders.")
    opening_time = models.TimeField(help_text="When the restaurant opens (e.g., 08:00:00)", default="00:00")
    closing_time = models.TimeField(help_text="When the restaurant closes (e.g., 22:00:00)", default="00:00")

    is_subscribed = models.BooleanField(default=False)
    subscription_expires_at = models.DateTimeField(null=True, blank=True)

    # 7-Day Free Trial fields
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    has_used_free_trial = models.BooleanField(default=False)

    @property
    def is_currently_open(self):
        """
        Dynamically calculates if the restaurant is currently open.
        Handles standard days as well as overnight shifts.
        """
        now_time = timezone.localtime(timezone.now()).time()

        if self.opening_time <= self.closing_time:
            return self.opening_time <= now_time <= self.closing_time
        else:
            return now_time >= self.opening_time or now_time <= self.closing_time

    @property
    def active_subscription(self):
        """
        Safely retrieves the associated subscription record
        without crashing if the row doesn't exist.
        """
        try:
            return self.vendorsubscription
        except AttributeError:
            return None

    @property
    def subscription_or_trial_status(self):
        return evaluate_merchant_subscription_or_trial(self)

    @property
    def has_active_access(self) -> bool:
        return self.subscription_or_trial_status.has_access

    @property
    def is_on_active_trial(self) -> bool:
        return self.subscription_or_trial_status.is_on_trial

    @property
    def trial_days_remaining(self) -> int:
        return self.subscription_or_trial_status.trial_days_remaining

    @property
    def can_accept_orders(self) -> bool:
        return bool(
            self.is_approved
            and self.is_active_store
            and self.has_active_access
            and self.is_currently_open
        )

    def recalculate_ratings(self):
        """Recalculates aggregate star counts and average rating score."""
        reviews = self.reviews.all()
        total_count = reviews.count()

        if total_count == 0:
            self.rating = 0.00
            self.review_count = 0
            self.count_5_stars = 0
            self.count_4_stars = 0
            self.count_3_stars = 0
            self.count_2_stars = 0
            self.count_1_stars = 0
        else:
            self.review_count = total_count
            self.count_5_stars = reviews.filter(rating=5).count()
            self.count_4_stars = reviews.filter(rating=4).count()
            self.count_3_stars = reviews.filter(rating=3).count()
            self.count_2_stars = reviews.filter(rating=2).count()
            self.count_1_stars = reviews.filter(rating=1).count()

            total_sum = sum(r.rating for r in reviews)
            self.rating = round(total_sum / total_count, 2)

        self.save(update_fields=[
            'rating', 'review_count',
            'count_5_stars', 'count_4_stars',
            'count_3_stars', 'count_2_stars', 'count_1_stars'
        ])

    def save(self, *args, **kwargs):
        if self.is_approved:
            # Grant 7-day free trial once upon approval if no active subscription/trial exists
            ensure_merchant_trial_on_approval(self, days=7)

            # Automatically trigger Paystack subaccount creation upon admin approval
            if not self.paystack_subaccount_code and hasattr(self, 'settlement_account'):
                self.settlement_account.create_paystack_subaccount()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.company_name} ({self.get_business_type_display() or 'Individual Account'})"


class VendorShippingPreference(models.Model):
    """How a vendor wants orders delivered: by Aika riders, by themselves, or both."""

    class FulfillmentType(models.TextChoices):
        PLATFORM_LOGISTICS = 'platform', 'Platform Logistics (Aika Riders)'
        SELF_DELIVERY = 'self', 'Self Delivery (Vendor Handled)'
        HYBRID = 'hybrid', 'Both Options Available'

    vendor = models.OneToOneField(MerchantProfile, on_delete=models.CASCADE, related_name='shipping_preference')
    fulfillment_type = models.CharField(
        max_length=20,
        choices=FulfillmentType.choices,
        default=FulfillmentType.PLATFORM_LOGISTICS
    )
    # (A trailing comma after this field used to turn it into a tuple, so no column was created.)
    allow_nationwide = models.BooleanField(
        default=False,
        help_text="Platform-only vendors: also accept buyers outside the vendor's own hub."
    )
    # Delivery charge the vendor sets for orders they deliver themselves (0 = free delivery).
    self_delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.vendor_id}: {self.fulfillment_type}"


class MerchantReview(models.Model):
    """
    Handles customer reviews and star ratings for merchant profiles.
    Automatically updates the aggregate rating and star distribution counts on the MerchantProfile.
    """
    merchant = models.ForeignKey(
        'MerchantProfile',
        on_delete=models.CASCADE,
        related_name='reviews'
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='merchant_reviews'
    )
    rating = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Star rating from 1 to 5"
    )
    comment = models.TextField(blank=True, null=True, help_text="Optional customer review feedback")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('merchant', 'customer')  # Ensures a customer can only review a merchant once
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.rating} Stars for {self.merchant.company_name} by {self.customer.email}"

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            self.merchant.recalculate_ratings()

    def delete(self, *args, **kwargs):
        merchant = self.merchant
        super().delete(*args, **kwargs)
        merchant.recalculate_ratings()

def storefront_banner_upload_path(instance, filename):
    """
    Generates a clean, isolated upload pipeline path for storefront assets.
    Example output: media/storefronts/wf-a38f7e2k/hero_banners/image.jpg
    """
    ext = filename.split('.')[-1]
    clean_filename = f"hero_banner.{ext}"
    return os.path.join('storefronts', str(instance.profile.id).lower(), 'branding', clean_filename)

class StorefrontConfig(models.Model):
    """
    Extends CustomerProfile specifically for E-commerce retail storefront customization.
    Houses public brand details, marketing banners, and appearance matrices.
    """
    profile = models.OneToOneField(
        'MerchantProfile',
        on_delete=models.CASCADE,
        related_name='storefront_config',
        limit_choices_to={'business_type': 'e_commerce'},
        help_text="The core vendor business profile associated with this storefront design configuration."
    )
    store_slug = models.SlugField(
        max_length=255, 
        unique=True, 
        blank=True,
        help_text="The URL-friendly tracking handle for the public menu link (e.g., wegophera.com/store/techforge-hub/)"
    )
    hero_image = models.ImageField(
        upload_to=storefront_banner_upload_path,
        blank=True,
        null=True,
        help_text="The primary hero image/banner displayed at the top of your public storefront page. Recommended resolution: 1200x400px."
    )
    tagline = models.CharField(
        max_length=255, 
        blank=True, 
        help_text="A short, catchy slogan displayed directly beneath your store brand title."
    )
    brand_bio = models.TextField(
        blank=True, 
        help_text="A brief summary detailing your brand mission, specialty values, or warranty frameworks."
    )
    
    # Optional social infrastructure hooks to boost public conversion channels
    instagram_handle = models.CharField(max_length=100, blank=True, help_text="Handle without the @ symbol")
    twitter_handle = models.CharField(max_length=100, blank=True, help_text="Handle without the @ symbol")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        """Automatically enforces an absolute clean slug system based on the company name."""
        if not self.store_slug and self.profile.company_name:
            base_slug = slugify(self.profile.company_name)
            slug = base_slug
            counter = 1
            # Prevent collision loops if multiple stores share a name structure
            while StorefrontConfig.objects.filter(store_slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.store_slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Storefront Config for {self.profile.company_name}"