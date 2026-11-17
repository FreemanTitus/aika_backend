from django.db import models
from decimal import Decimal

class PlatformLedger(models.Model):
    """
    Tracks platform revenue generated from logistics order cuts and other platform earnings.
    """
    class RevenueSource(models.TextChoices):
        LOGISTICS_CUT = 'logistics_cut', 'Logistics Platform Cut'
        SUBSCRIPTION = 'subscription', 'Merchant Subscription'
        OTHER = 'other', 'Other Revenue'

    reference = models.CharField(
        max_length=100, 
        unique=True, 
        null=True, 
        blank=True,
        help_text="Unique transaction or order reference tag."
    )
    source_type = models.CharField(
        max_length=30, 
        choices=RevenueSource.choices,
        default=RevenueSource.LOGISTICS_CUT
    )
    amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        help_text="Monetary amount earned by the platform."
    )
    description = models.TextField(
        blank=True, 
        null=True, 
        help_text="Explanation of the revenue entry."
    )
    logistics_order = models.ForeignKey(
        'order.Order', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='platform_ledger_entries',
        help_text="Associated logistics delivery order if applicable."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        """Generates a tracking reference if missing."""
        if not self.reference:
            from accounts.utils import generate_alphanumeric_id  # Adjust based on your helper path
            self.reference = f"PL-TRX-{generate_alphanumeric_id(8)}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"+₦{self.amount} ({self.get_source_type_display()}) - Ref: {self.reference}"