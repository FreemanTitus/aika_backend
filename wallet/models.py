import random
import requests
from django.conf import settings
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password


User = get_user_model()

class VendorWallet(models.Model):
    profile = models.OneToOneField(
        'merchant.MerchantProfile', 
        on_delete=models.CASCADE, 
        related_name='vendor_wallet',
    )
    # Money ready for immediate withdrawal via OPay payout
    available_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    # Money locked until the rider successfully delivers the package
    escrow_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Secure hashed 4-digit transfer PIN
    transfer_pin = models.CharField(max_length=128, blank=True, null=True)

    pin_failed_attempts = models.PositiveSmallIntegerField(default=0)
    pin_locked_until = models.DateTimeField(null=True, blank=True)
    security_hold_until = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.profile.company_name} Wallet (Avail: ₦{self.available_balance})"

    def set_pin(self, raw_pin: str):
        """Hash and save the 4-digit transfer PIN."""
        self.transfer_pin = make_password(raw_pin)
        self.save(update_fields=['transfer_pin'])

    def verify_pin(self, raw_pin: str) -> bool:
        """Verify if the provided raw PIN matches the stored hash."""
        if not self.transfer_pin:
            return False
        return check_password(raw_pin, self.transfer_pin)


class PaymentSettlementAccount(models.Model):
    # CHANGED: Swapped from User to CustomerProfile OneToOne mapping
    profile = models.OneToOneField(
        'merchant.MerchantProfile', 
        on_delete=models.CASCADE, 
        related_name='settlement_account',
    )
    bank_name = models.CharField(max_length=100)
    bank_code = models.CharField(
        max_length=10, 
        blank=True, 
        null=True, 
        help_text="The unique central routing numerical bank code (e.g., '044', '950')."
    )
    account_number = models.CharField(max_length=10)
    account_name = models.CharField(max_length=255, help_text="Validated dynamically via OPay account lookups")
    is_verified = models.BooleanField(default=False)
    recipient_code = models.CharField(max_length=50, null=True, blank=True)

    def __str__(self):
        return f"{self.bank_name} - {self.account_number} ({self.profile.company_name})"

class PayoutLog(models.Model):
    class PayoutStatus(models.TextChoices):
        PENDING = 'pending', 'Pending Approval'
        PROCESSING = 'processing', 'Processing with OPay'
        SUCCESS = 'success', 'Settled Successfully'
        FAILED = 'failed', 'Transfer Failed'

    wallet = models.ForeignKey(VendorWallet, on_delete=models.CASCADE, related_name='payouts')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=50, unique=True, help_text="Unique OPay merchant reference string")
    status = models.CharField(max_length=20, choices=PayoutStatus.choices, default=PayoutStatus.PENDING)
    opay_order_id = models.CharField(max_length=100, blank=True, null=True, help_text="Returned from OPay transfer API")

    balance_restored = models.BooleanField(default=False)           # makes refunds idempotent
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Payout {self.reference} - ₦{self.amount} ({self.status})"