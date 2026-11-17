import random
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings
from accounts.utils import generate_alphanumeric_id
from decimal import Decimal
from core.models import DeliveryConfig


class RiderProfile(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        BUSY = 'busy', 'Busy'
        OFFLINE = 'offline', 'Offline'
    
    class EmploymentType(models.TextChoices):
        FULL_TIME = 'full_time', 'Full-Time (Fixed Rate per Delivery)'
        CASUAL = 'casual', 'Casual / Part-Time (Threshold Revenue Share)'

    class DeviceType(models.TextChoices):
        SMARTPHONE = 'smartphone', 'Smartphone (Android / iPhone)'
        FEATURE_PHONE = 'feature_phone', 'Basic Phone (SMS / Calls Only)'

    class PayoutSchedule(models.TextChoices):
        DAILY = 'daily', 'Daily Payout'
        WEEKLY = 'weekly', 'Weekly Payout'
        MONTHLY = 'monthly', 'Monthly Payout'

    class PayoutMethod(models.TextChoices):
        CASH = 'cash', 'Cash'
        ACCOUNT = 'account', 'Bank Account'

    class IDType(models.TextChoices):
        NIN = 'nin', 'National Identification Number (NIN)'
        BVN = 'drivers_license', 'Driver’s License'
        VOTERS_CARD = 'voters_card', 'Voter’s Card'

    id = models.CharField(primary_key=True, max_length=20, editable=False)
    user = models.OneToOneField(
        'accounts.CustomUser', 
        on_delete=models.CASCADE, 
        related_name='rider_profile',
        null=True,
        blank=True,
    )
    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255, default="")

    # Employment & Payout Structure Selection
    employment_type = models.CharField(
        max_length=20,
        choices=EmploymentType.choices,
        default=EmploymentType.FULL_TIME,
        help_text="Determines whether rider earns fixed per-order rate or threshold revenue share."
    )
    payout_schedule = models.CharField(
        max_length=15, 
        choices=PayoutSchedule.choices, 
        default=PayoutSchedule.DAILY
    )
    payout_method = models.CharField(
        max_length=10, 
        choices=PayoutMethod.choices, 
        default=PayoutMethod.ACCOUNT
    )

    id_type = models.CharField(max_length=30, choices=IDType.choices, blank=True, null=True)
    id_number = models.CharField(max_length=50, blank=True, null=True)
    id_document_image = models.ImageField(upload_to='riders/ids/', blank=True, null=True)
    passport_photograph = models.ImageField(upload_to='riders/portraits/', blank=True, null=True)
    is_id_verified = models.BooleanField(default=False)
    
    # Emergency / Guarantor details for bike security tracking
    guarantor_name = models.CharField(max_length=255, blank=True, null=True)
    guarantor_phone = models.CharField(max_length=20, blank=True, null=True)
    guarantor_address = models.TextField(blank=True, null=True)
    
    bike_plate_number = models.CharField(max_length=20, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    device_type = models.CharField(
        max_length=20,
        choices=DeviceType.choices,
        default=DeviceType.SMARTPHONE,
        help_text="Tracks whether this delivery agent receives automated app routes or manual SMS updates."
    )
    rating = models.DecimalField(
        max_digits=3, 
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(1.0), MaxValueValidator(5.0)]
    )
    review_count = models.PositiveIntegerField(default=0)
    is_vetted = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def recalculate_ratings(self):
        """Recalculates the rider's average rating and total review count."""
        reviews = self.reviews.all()
        self.review_count = reviews.count()
        if self.review_count > 0:
            avg = reviews.aggregate(models.Avg('rating'))['rating__avg']
            self.rating = round(avg, 2) if avg else 0.00
        else:
            self.rating = 0.00
        self.save(update_fields=['rating', 'review_count'])

    def save(self, *args, **kwargs):
        """Generates a secure tracking key before storing the record instance."""
        if not self.id:
            while True:
                potential_id = f"RP-{generate_alphanumeric_id(8)}"
                if not RiderProfile.objects.filter(id=potential_id).exists():
                    self.id = potential_id
                    break
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        """Combines first and last name for display purposes."""
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return f"{self.full_name} ({self.bike_plate_number}) - {self.get_employment_type_display()}"


class RiderReview(models.Model):
    """
    Handles customer reviews and star ratings for rider profiles.
    Automatically updates the aggregate rating and star distribution counts on the RiderProfile.
    """
    rider = models.ForeignKey(
        'RiderProfile',
        on_delete=models.CASCADE,
        related_name='reviews'
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='rider_reviews'
    )
    rating = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Star rating from 1 to 5"
    )
    comment = models.TextField(blank=True, null=True, help_text="Optional customer review feedback for the rider")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('rider', 'customer')  # Ensures a customer can only review a specific rider once
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.rating} Stars for {self.rider.full_name} by {self.customer.email}"

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            self.rider.recalculate_ratings()

    def delete(self, *args, **kwargs):
        rider = self.rider
        super().delete(*args, **kwargs)
        rider.recalculate_ratings()


class FCMDeviceToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fcm_tokens")
    token = models.TextField(unique=True) # Holds the long Firebase browser token string
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - Device Token"


class RiderTelemetryLog(models.Model):
    """
    Stores chronological GPS pings emitted by riders during active delivery transits.
    """
    order = models.ForeignKey(
        'order.Order', 
        on_delete=models.CASCADE, 
        related_name="telemetry_logs",
        help_text="The specific active delivery route being mapped"
    )
    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rider_pings"
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['order', 'timestamp']),
        ]

    def __str__(self):
        return f"Rider {self.rider.id} Log for Order {self.order.id} at {self.timestamp}"


class RiderLocation(models.Model):
    rider = models.OneToOneField(RiderProfile, on_delete=models.CASCADE, related_name='live_location')
    latitude = models.FloatField(default=0.0)
    longitude = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Rider {self.rider.email} -> Lat: {self.latitude}, Lng: {self.longitude}"


class RiderPaymentAccount(models.Model):
    rider = models.OneToOneField(
        RiderProfile, 
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
    account_name = models.CharField(max_length=255, help_text="Validated dynamically via bank lookups")
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.bank_name} - {self.account_number} ({self.rider.first_name})"


class RiderWallet(models.Model):
    """
    Manages the overall financial balance and earnings for a delivery rider.
    """
    rider = models.OneToOneField(
        RiderProfile, 
        on_delete=models.CASCADE, 
        related_name='wallet'
    )
    balance = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        default=Decimal('0.00'),
        help_text="Current spendable or withdrawable balance."
    )
    total_earned = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        default=Decimal('0.00'),
        help_text="Lifetime cumulative earnings for the rider."
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet for {self.rider.full_name} - ₦{self.balance}"


class RiderTransactionLedger(models.Model):
    """
    Logs every financial credit or debit entry associated with a rider's wallet.
    """
    class TransactionType(models.TextChoices):
        EARNING = 'earning', 'Delivery Earning'
        WITHDRAWAL = 'withdrawal', 'Wallet Withdrawal'
        BONUS = 'bonus', 'Platform Bonus'
        REFUND = 'refund', 'Adjustment / Refund'

    rider = models.ForeignKey(
        RiderProfile, 
        on_delete=models.CASCADE, 
        related_name='transactions'
    )
    amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        help_text="Transaction monetary amount."
    )
    transaction_type = models.CharField(
        max_length=20, 
        choices=TransactionType.choices,
        default=TransactionType.EARNING
    )
    description = models.TextField(
        blank=True, 
        null=True, 
        help_text="Details explaining the source or reason for this entry."
    )
    reference = models.CharField(
        max_length=100, 
        unique=True, 
        null=True, 
        blank=True,
        help_text="Unique transaction or order reference tag."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        """Generates a tracking reference if missing."""
        if not self.reference:
            from accounts.utils import generate_alphanumeric_id  # Adjust import based on your helper function location
            self.reference = f"TRX-{generate_alphanumeric_id(8)}"
        super().save(*args, **kwargs)

    def __str__(self):
        sign = "+" if self.transaction_type in [self.TransactionType.EARNING, self.TransactionType.BONUS] else "-"
        return f"{self.rider.full_name}: {sign}₦{self.amount} ({self.get_transaction_type_display()})"