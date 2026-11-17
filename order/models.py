import random
from django.db import models
from accounts.models import CustomUser
from core.models import DeliveryConfig
from decimal import Decimal
from rider.models import RiderProfile



class Order(models.Model):
    """
    The core Engine. Supports registered businesses, logged-in individuals, 
    and one-off guest users who want a bike urgently.
    """
    class OrderStatus(models.TextChoices):
        PENDING = 'pending', 'Pending Admin Assignment'
        ACKNOWLEDGED = 'acknowledged', 'Acknowledged by Vendor'
        ASSIGNED = 'assigned', 'Rider Assigned'
        IN_TRANSIT = 'in_transit', 'In Transit'
        DELIVERED = 'delivered', 'Delivered'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    class PackageType(models.TextChoices):
        FOOD = 'food', 'Food / Perishables'
        DOCUMENTS = 'documents', 'Documents'
        CLOTHING = 'clothing', 'Clothing & Accessories'
        ELECTRONICS = 'electronics', 'Electronics'
        OTHER = 'other', 'Other'

    order_reference = models.CharField(max_length=15, unique=True, editable=False, null=True, blank=True)
    
    # Linked Sender: null=True allows guest checkout from your homepage without login
    sender = models.ForeignKey('merchant.MerchantProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders_sent')
    receiver = models.ForeignKey('customers.IndividualProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders_receiver')
    marketplace_order = models.ForeignKey(
        'marketplace.MarketplaceOrder',  # Using string reference to avoid circular import blocks
        on_delete=models.SET_NULL,        # If a marketplace order is cleared, keep the logistics history log intact
        blank=True,
        null=True,
        related_name='logistics_dispatches'
    )
    
    # Explicit Sender info (Filled automatically via profile if logged in, or typed manually if Guest)
    sender_name = models.CharField(max_length=255, default="", help_text="Sender's name (auto-filled for logged-in users)")
    sender_phone = models.CharField(max_length=20, default="", help_text="Sender's phone number")
    pickup_address = models.TextField(default="", help_text="Where the rider meets the sender to pick up the package")

    # Receiver details
    receiver_name = models.CharField(max_length=255, default="", help_text="Receiver's name")
    receiver_phone = models.CharField(max_length=20, default="", help_text="Receiver's phone number")
    delivery_address = models.TextField(default="", help_text="Where the package is to be delivered")

    # Package Details
    package_type = models.CharField(max_length=30, choices=PackageType.choices, default=PackageType.OTHER, null=True, blank=True)
    package_description = models.TextField(blank=True, null=True, help_text="e.g., 2 boxes of pizza, or iPhone box")

    # Financials
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2)
    is_cash_on_delivery = models.BooleanField(default=False)
    cod_amount_to_collect = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Amount rider must collect from receiver")

    # Dynamic Financial Splits (Captured from DeliveryConfig on creation)
    rider_earning = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))
    platform_fee = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))

    # Dispatch Tracking
    assigned_rider = models.ForeignKey(
        'rider.RiderProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    def calculate_and_set_splits(self):
        """Computes rider earnings and platform fee based on the rider's employment type."""
        if not self.assigned_rider:
            return

        config = DeliveryConfig.get_solo()

        if self.assigned_rider.employment_type == RiderProfile.EmploymentType.FULL_TIME:
            # Full-time: Fixed payout (e.g., ₦1,000)
            self.rider_earning = config.full_time_rider_earning
            # Platform keeps the remaining balance of the delivery fee (or handles it as net)
            self.platform_fee = max(Decimal('0.00'), self.delivery_fee - self.rider_earning)

        elif self.assigned_rider.employment_type == RiderProfile.EmploymentType.CASUAL:
            # Casual: Revenue share percentage pool split (e.g., Platform takes 20%, Rider gets 80%)
            platform_cut_percentage = config.casual_platform_percentage / Decimal('100.00')
            self.platform_fee = (self.delivery_fee * platform_cut_percentage).quantize(Decimal('0.01'))
            self.rider_earning = self.delivery_fee - self.platform_fee

    def save(self, *args, **kwargs):
        # 1. Generate unique order reference if missing
        if not self.order_reference:
            while True:
                potential_ref = ''.join([str(random.randint(0, 9)) for _ in range(15)])
                if not Order.objects.filter(order_reference=potential_ref).exists():
                    self.order_reference = potential_ref
                    break

        # 2. Automatically calculate splits if a rider is present and fees haven't been locked yet
        if self.assigned_rider and (self.rider_earning == 0 or self.platform_fee == 0):
            self.calculate_and_set_splits()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order {self.order_reference} - [{self.status}]"


class UserCartItem(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='cart_items')
    product_id = models.CharField(max_length=100)
    quantity = models.PositiveIntegerField(default=1)
    store_id = models.CharField(max_length=100, blank=True, null=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    vendor_name = models.CharField(max_length=255, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'product_id')