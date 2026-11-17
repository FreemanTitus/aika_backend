from django.db import models
from decimal import Decimal
from django.core.cache import cache
from django.core.exceptions import ValidationError


class PlatformSetting(models.Model):
    """
    Singleton model to manage platform-wide referral thresholds and reward bonuses dynamically.
    Only one row should ever exist in this table.
    """
    referral_order_threshold = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        default=Decimal('5000.00'),
        help_text="Minimum order spend required for an invitee's order to unlock the referral reward."
    )
    referral_bonus_amount = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        default=Decimal('500.00'),
        help_text="Wallet bonus amount credited to the inviter when the referral task is completed."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Platform Setting"
        verbose_name_plural = "Platform Settings"

    def save(self, *args, **kwargs):
        # Enforce singleton pattern (always force ID = 1)
        self.pk = 1
        super().save(*args, **kwargs)
        # Clear cache whenever settings are updated in Django Admin
        cache.delete('platform_settings_cache')

    @classmethod
    def get_settings(cls):
        """
        Retrieves settings from cache or database to avoid unnecessary DB hits on every order.
        """
        settings_obj = cache.get('platform_settings_cache')
        if not settings_obj:
            settings_obj, _ = cls.objects.get_or_create(pk=1)
            cache.set('platform_settings_cache', settings_obj, timeout=None) # Cache indefinitely until saved
        return settings_obj

    def __str__(self):
        return "Global Platform & Referral Settings"


class DeliveryConfig(models.Model):
    """Singleton configuration for global platform delivery economics."""
    
    # Full-Time Settings (Fixed payout per order)
    full_time_rider_earning = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('1000.00'),
        help_text="Fixed amount paid to full-time riders per delivery."
    )
    
    # Casual / Part-Time Settings (Percentage cut, e.g., 20.00 for 20%)
    casual_platform_percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2, 
        default=Decimal('20.00'),
        help_text="Percentage of the delivery fee withheld by the platform for casual riders."
    )
    
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.pk and DeliveryConfig.objects.exists():
            raise ValidationError("There can only be one DeliveryConfig instance.")
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        config, _ = cls.objects.get_or_create(pk=1, defaults={
            'full_time_rider_earning': Decimal('1000.00'),
            'casual_platform_percentage': Decimal('20.00'),
        })
        return config

    def __str__(self):
        return f"Delivery Config (FT Rider Rate: ₦{self.full_time_rider_earning} | Casual Platform Cut: {self.casual_platform_percentage}%)"