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
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True, blank=True, help_text="Unique URL identifier generated from name.")
    price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Monthly subscription cost in NGN")
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
        return f"{self.name} - ₦{self.price}"
    
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
        # Splits by newline or comma if present, filtering out blank spaces
        separator = '\n' if '\n' in self.features_raw else ','
        return [item.strip() for item in self.features_raw.split(separator) if item.strip()]
    
    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            # Collision prevention loop check
            while SubscriptionPlan.objects.filter(slug=slug).exists():
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
        """Calculates 30 days before the subscription expires as the start date of the current billing cycle."""
        if self.subscription_expires_at:
            return self.subscription_expires_at - timezone.timedelta(days=30)
        return timezone.now() - timedelta(days=30)

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
    