from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from decimal import Decimal


class SubscriptionInvoice(models.Model):
    class PaymentStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SUCCESSFUL = 'successful', 'Successful'
        FAILED = 'failed', 'Failed'

    vendor_profile = models.ForeignKey('merchant.MerchantProfile', on_delete=models.CASCADE, related_name='subscription_invoices')
    plan = models.ForeignKey('SubscriptionPlan', on_delete=models.PROTECT)
    reference_id = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    payment_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.reference_id} - {self.plan.name} ({self.status})"
    
    
class MarketplaceTransaction(models.Model):
    order_id = models.CharField(max_length=100, unique=True)
    vendor_profile = models.ForeignKey('merchant.MerchantProfile', on_delete=models.CASCADE)
    total_basket_value = models.DecimalField(max_digits=12, decimal_places=2)
    
    # 2% Platform Collection Cut Tracking
    platform_fee_collected = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vendor_payout_share = models.DecimalField(max_digits=12, decimal_places=2)
    
    paystack_reference = models.CharField(max_length=100, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)


class SubscriptionPlan(models.Model):
    """
    Stores platform-wide pricing tiers dynamically managed via the admin panel.
    """

    class BillingInterval(models.TextChoices):
        MONTHLY = "monthly", "Monthly (30 Days)"
        QUARTERLY = "quarterly", "Quarterly (90 Days)"
        SEMI_ANNUAL = "semi_annual", "6 Months (180 Days)"
        ANNUAL = "annual", "Annually (365 Days)"

    INTERVAL_DAYS = {
        BillingInterval.MONTHLY: 30,
        BillingInterval.QUARTERLY: 90,
        BillingInterval.SEMI_ANNUAL: 180,
        BillingInterval.ANNUAL: 365,
    }

    name = models.CharField(max_length=100)
    slug = models.SlugField(
        max_length=120,
        unique=True,
        blank=True,
        help_text="Unique URL identifier generated from name.",
    )
    billing_interval = models.CharField(
        max_length=20,
        choices=BillingInterval.choices,
        default=BillingInterval.MONTHLY,
        db_index=True,
        help_text="Billing cycle length for this plan tier.",
    )
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Subscription cost in NGN for the selected billing interval",
    )
    description = models.TextField(blank=True, null=True)

    # --- Quantitative Limits ---
    max_monthly_orders = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max orders per billing cycle. Leave blank or None for unlimited.",
    )
    max_products = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max active catalog products. Leave blank for unlimited.",
    )

    # --- Feature Perks / Flags ---
    has_priority_dispatch = models.BooleanField(
        default=False,
        help_text="Riders are prioritized for orders from vendors on this plan.",
    )
    has_featured_placement = models.BooleanField(
        default=False,
        help_text="Vendor appears in the 'Featured' / top carousel on search.",
    )
    has_advanced_analytics = models.BooleanField(
        default=False,
        help_text="Access to sales forecasting and customer retention reports.",
    )
    features_raw = models.TextField(
        help_text="Enter features separated by newlines or commas (e.g., Feature 1, Feature 2)"
    )
    is_available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.get_billing_interval_display()}) - ₦{self.price}"

    @property
    def duration_days(self) -> int:
        return self.INTERVAL_DAYS.get(self.billing_interval, 30)

    @property
    def is_unlimited_orders(self):
        return self.max_monthly_orders is None

    @property
    def features_list(self):
        """
        Parses features text block into a clean iterable list for templates.
        """
        if not self.features_raw:
            return []
        separator = "\n" if "\n" in self.features_raw else ","
        return [item.strip() for item in self.features_raw.split(separator) if item.strip()]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(f"{self.name}-{self.billing_interval}")
            slug = base_slug
            counter = 1
            while SubscriptionPlan.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)
    

class VendorSubscription(models.Model):
    vendor_profile = models.OneToOneField('merchant.MerchantProfile', on_delete=models.CASCADE)
    #Linked plan relationship tier
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True)
    is_active = models.BooleanField(default=False)
    subscription_expires_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.vendor_profile.user.username} - {self.plan.name if self.plan else 'No Active Plan'}"

    @property
    def has_valid_access(self):
        """Checks if the subscription is active and not expired."""
        if self.is_active and self.subscription_expires_at:
            return self.subscription_expires_at > timezone.now()
        return False

    @property
    def billing_cycle_starts(self):
        """Calculates the start of the current billing window using the active plan's duration."""
        cycle_days = self.plan.duration_days if self.plan else 30
        if self.subscription_expires_at:
            return self.subscription_expires_at - timedelta(days=cycle_days)
        return timezone.now() - timedelta(days=cycle_days)

    def get_orders_this_cycle_count(self):
        """Calculates the number of marketplace orders placed with the vendor within the current billing cycle."""
        return self.vendor_profile.vendor_orders.filter(
            created_at__gte=self.billing_cycle_starts,
        ).count()

    @property                                                                                                                      
    def can_accept_orders(self):                                                                                                   
        """                                                                                                                        
        Validates both active subscription and plan monthly order threshold.                                                       
        """                                                                                                                        
        if not self.has_valid_access or not self.plan:                                                                             
            return False                                                                                                           
                                                                                                                                    
        # Unlimited orders plan (e.g. Pro / Enterprise)                                                                            
        if self.plan.is_unlimited_orders:                                                                                          
            return True                                                                                                            
                                                                                                                                    
        # Check current order count against tier limit (e.g. 100 for Basic)                                                        
        return self.get_orders_this_cycle_count() < self.plan.max_monthly_orders                                                   
                                                                                                                                    
    @property                                                                                                                      
    def remaining_orders(self):                                                                                                    
        """Returns remaining orders count or None if unlimited."""                                                                 
        if not self.plan or self.plan.is_unlimited_orders:                                                                         
            return None                                                                                                            
        return max(0, self.plan.max_monthly_orders - self.get_orders_this_cycle_count())                                           
                                                                                                                                    
    # --- Convenience Feature Accessors ---                                                                                        
    @property                                                                                                                      
    def has_priority_dispatch(self):                                                                                               
        return self.has_valid_access and bool(self.plan and self.plan.has_priority_dispatch)                                       
                                                                                                                                    
    @property                                                                                                                      
    def has_featured_placement(self):                                                                                              
        return self.has_valid_access and bool(self.plan and self.plan.has_featured_placement)   

    @property
    def can_add_product(self):
        """
        Validates whether the vendor can add more products based on their subscription tier limit.
        """
        if not self.has_valid_access or not self.plan:
            return False

        # Unlimited products if limit is None
        if self.plan.max_products is None:
            return True

        current_product_count = self.vendor_profile.products.count()
        return current_product_count < self.plan.max_products

    @property
    def days_until_expiry(self):
        """Returns remaining full/fractional days until subscription expires, or None if unset."""
        if not self.subscription_expires_at:
            return None
        delta = self.subscription_expires_at - timezone.now()
        return max(0.0, delta.total_seconds() / 86400)

    @property
    def is_expiring_soon(self):
        """True when subscription is active and expires within the next 7 days."""
        if not self.has_valid_access or not self.subscription_expires_at:
            return False
        return self.subscription_expires_at <= timezone.now() + timedelta(days=7)

    @property
    def is_order_limit_low(self):
        """True when a capped plan has 10 or fewer remaining orders (or <= 10% left)."""
        rem = self.remaining_orders
        if rem is None or not self.plan or not self.plan.max_monthly_orders:
            return False
        threshold = max(1, int(self.plan.max_monthly_orders * 0.1))
        return rem <= min(10, threshold)

    def activate_plan(self, plan, days=None):
        """Activates or renews a subscription plan cleanly using its billing interval."""
        now = timezone.now()
        effective_days = days if days is not None else plan.duration_days
        base_time = (
            self.subscription_expires_at
            if self.is_active and self.subscription_expires_at and self.subscription_expires_at > now else now
        )
        self.plan = plan
        self.is_active = True
        self.subscription_expires_at = base_time + timedelta(days=effective_days)
        self.save(update_fields=["plan", "is_active", "subscription_expires_at"])


class SubscriptionReminderLog(models.Model):
    class ReminderType(models.TextChoices):
        EXPIRING_7_DAYS = 'expiring_7_days', 'Expiring in 7 days'
        EXPIRING_3_DAYS = 'expiring_3_days', 'Expiring in 3 days'
        EXPIRING_1_DAY = 'expiring_1_day', 'Expiring in 1 day'
        EXPIRED = 'expired', 'Subscription expired'
        ORDER_LIMIT_LOW = 'order_limit_low', 'Order limit nearly reached'
        UNUSED_AD_BOOST = 'unused_ad_boost', 'Unused ad boost available'

    subscription = models.ForeignKey(
        'VendorSubscription',
        on_delete=models.CASCADE,
        related_name='reminder_logs',
    )
    reminder_type = models.CharField(max_length=32, choices=ReminderType.choices)
    cycle_expires_at = models.DateTimeField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['subscription', 'reminder_type', 'cycle_expires_at'],
                name='unique_subscription_reminder_per_cycle',
            )
        ]
