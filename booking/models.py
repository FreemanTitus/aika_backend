from django.db import models
from django.conf import settings
import random


import random
from django.db import models
from django.conf import settings

class AbstractBooking(models.Model):
    """
    Abstract base model containing common fields for both rides and package deliveries.
    """
    class BookingStatus(models.TextChoices):
        PENDING = 'pending', 'Pending Admin Assignment'
        ASSIGNED = 'assigned', 'Rider Assigned'
        IN_TRANSIT = 'in_transit', 'In Transit'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    booking_reference = models.CharField(max_length=15, unique=True)
    
    # Who is making the request (Null if requested anonymously from homepage)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='%(class)s_bookings'
    )
    
    # Core contact information
    customer_name = models.CharField(max_length=255, help_text="Passenger name or Sender name")
    customer_phone = models.CharField(max_length=20)
    
    # Geographic locations
    pickup_address = models.TextField(default="")
    dropoff_address = models.TextField(default="")

    # Financials & Dispatch Info
    fare_amount = models.DecimalField(max_digits=10, decimal_places=2, help_text="Total price of the ride or delivery")
    assigned_rider = models.ForeignKey(
        'rider.RiderProfile', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='%(class)s_assigned'
    )
    status = models.CharField(max_length=20, choices=BookingStatus.choices, default=BookingStatus.PENDING)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['booking_reference']),
            models.Index(fields=['status', 'created_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.booking_reference:
            while True:
                potential_ref = ''.join([str(random.randint(0, 9)) for _ in range(15)])
                # Ensures uniqueness across the specific concrete model table
                if not self.__class__.objects.filter(booking_reference=potential_ref).exists():
                    self.booking_reference = potential_ref
                    break
        super().save(*args, **kwargs)


class RideBooking(AbstractBooking):
    """
    Handles cross-town passenger motorcycle bookings.
    """
    # Add trip-specific fields here if needed in the future (e.g., helmet requested, passenger count)

    def __str__(self):
        return f"TRIP {self.booking_reference} - {self.status}"


class SendPackage(AbstractBooking):
    """
    Handles merchant and individual package deliveries / errand loops.
    """
    receiver_name = models.CharField(max_length=255)
    receiver_phone = models.CharField(max_length=20)
    package_description = models.TextField(help_text="e.g., Laptop, Food parcels")
    is_cash_on_delivery = models.BooleanField(default=False)
    cod_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    def __str__(self):
        return f"DELIVERY {self.booking_reference} - {self.status}"
