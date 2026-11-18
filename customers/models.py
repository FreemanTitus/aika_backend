from django.db import models
from django.conf import settings
import uuid
from decimal import Decimal


class IndividualProfile(models.Model):
    """
    Dedicated profile strictly for regular consumers ordering on the platform.
    """

    class IDType(models.TextChoices):
        NIN = 'nin', 'National Identification Number (NIN)'
        BVN = 'bvn', 'Bank Verification Number (BVN)'
        VOTERS_CARD = 'voters_card', 'Voter’s Card'

    id = models.CharField(primary_key=True, max_length=20, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='individual_profile'
    )
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    id_type = models.CharField(max_length=30, choices=IDType.choices, blank=True, null=True)
    id_number = models.CharField(max_length=50, blank=True, null=True)
    id_document_image = models.ImageField(upload_to='riders/ids/', blank=True, null=True)
    passport_photograph = models.ImageField(upload_to='riders/portraits/', blank=True, null=True)
    is_id_verified = models.BooleanField(default=False)
    
    default_delivery_address = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    referral_code = models.CharField(max_length=12, unique=True, blank=True, null=True)
    referred_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals_made')
    referral_reward_unlocked = models.BooleanField(default=False)

    @property
    def full_name(self) -> str:
        """Dynamically returns the combined full name."""
        return f"{self.first_name} {self.last_name}".strip()

    def save(self, *args, **kwargs):
        if not self.referral_code:
            # Generate a unique 8-character alphanumeric referral code
            while True:
                code = f"AIKA-{uuid.uuid4().hex[:6].upper()}"
                if not IndividualProfile.objects.filter(referral_code=code).exists():
                    self.referral_code = code
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} (Customer)"


class Referral(models.Model):
    inviter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_referrals')
    invitee = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='invitation_record')
    
    # Task completion milestones
    has_completed_qualifying_order = models.BooleanField(default=False)
    qualifying_order_id = models.CharField(max_length=50, blank=True, null=True)
    reward_granted = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.inviter.email} referred {self.invitee.email} (Reward: {self.reward_granted})"
    
class FrequentlyAskedQuestion(models.Model):
    question = models.CharField(max_length=500)
    answer = models.TextField()
    order = models.PositiveIntegerField(default=0)  # To control display sequence
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', '-created_at']

    def __str__(self):
        return self.question

class CustomerWallet(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    currency = models.CharField(max_length=3, default='NGN')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.email} - ₦{self.balance} ({self.currency})"


class CustomerWalletTransaction(models.Model):
    class TransactionType(models.TextChoices):
        REFERRAL_BONUS = 'referral_bonus', 'Referral Bonus'
        FUND_DEPOSIT = 'fund_deposit', 'Wallet Top-up (Future)'
        ORDER_PAYMENT = 'order_payment', 'Order Payment'
        DEBIT = 'debit', 'Wallet Debit'
        CREDIT = 'credit', 'Wallet Credit'

    wallet = models.ForeignKey(CustomerWallet, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=30, choices=TransactionType.choices)
    reference = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.transaction_type}: ₦{self.amount} for {self.wallet.user.email}"