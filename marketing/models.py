from django.db import models
from django.conf import settings

class SystemBroadcast(models.Model):
    class TargetAudience(models.TextChoices):
        ALL = 'all', 'All Users'
        VENDORS = 'vendors', 'All Vendors'
        RIDERS = 'riders', 'All Riders'
        CUSTOMERS = 'customers', 'All Customers'

    title = models.CharField(max_length=200)
    message = models.TextField()
    target_audience = models.CharField(max_length=20, choices=TargetAudience.choices, default=TargetAudience.ALL)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Broadcast: {self.title} ({self.target_audience})"


class UserNotification(models.Model):
    class NotificationType(models.TextChoices):
        REFERRAL_REWARD = 'referral_reward', 'Referral Reward'
        ORDER_UPDATE = 'order_update', 'Order Update'
        WALLET_CREDIT = 'wallet_credit', 'Wallet Credit'
        GENERAL = 'general', 'General'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    message = models.TextField()
    notification_type = models.CharField(max_length=30, choices=NotificationType.choices, default=NotificationType.GENERAL)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Notification for {self.user.email}: {self.title}"
    
    
class PromoCode(models.Model):
    class DiscountType(models.TextChoices):
        PERCENTAGE = 'percentage', 'Percentage (%)'
        FIXED = 'fixed', 'Fixed Amount (₦)'
        FREE_DELIVERY = 'free_delivery', 'Free Delivery'

    code = models.CharField(max_length=50, unique=True)
    discount_type = models.CharField(max_length=20, choices=DiscountType.choices, default=DiscountType.PERCENTAGE)
    value = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # e.g., 10% or ₦1000
    min_order_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    is_active = models.BooleanField(default=True)
    usage_limit = models.PositiveIntegerField(default=100)
    times_used = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Promo: {self.code} ({self.discount_type})"