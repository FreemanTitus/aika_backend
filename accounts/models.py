from django.db import models
from django.contrib.auth.models import AbstractUser
from .manager import CustomUserManager
import time
from geopy.geocoders import Nominatim


class State(models.Model):
    name = models.CharField(max_length=100, unique=True, help_text="e.g., Nasarawa")
    
    def __str__(self):
        return self.name


class CoverageArea(models.Model):
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name='coverage_areas')
    name = models.CharField(max_length=100, help_text="e.g., Akwanga, Keffi, Lafia")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_active = models.BooleanField(default=True, help_text="Designates if this hub is currently active.")

    class Meta:
        unique_together = ('state', 'name')

    def __str__(self):
        return f"{self.name}, {self.state.name}"

    def save(self, *args, **kwargs):
        # Automatically fetch coordinates if they are not provided
        if not self.latitude or not self.longitude:
            try:
                # Use a unique user_agent for your app
                geolocator = Nominatim(user_agent="aika_marketplace_geocoder")
                
                # Format the query strictly for Nigeria
                query = f"{self.name}, {self.state.name}, Nigeria"
                location = geolocator.geocode(query)
                
                if location:
                    self.latitude = location.latitude
                    self.longitude = location.longitude
                
                # Respect Nominatim's strict rate limit of 1 request per second
                time.sleep(1) 
            except Exception as e:
                print(f"Geocoding failed for {self.name}: {e}")
                
        super().save(*args, **kwargs)


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        STAFF = 'staff', 'Staff'
        AGENT = 'agent', 'Support Agent'
        BUSINESS = 'business', 'Business' 
        INDIVIDUAL = 'individual', 'Individual'
        RIDER = 'rider', 'Logistics Rider'

    # Using email as username configuration
    username = None 
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.INDIVIDUAL)
    phone_number = models.CharField(max_length=20)
    base_location = models.ForeignKey(
        'CoverageArea', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='users',
        help_text="Primary regional hub zone for marketplace operations"
    )
    street_address = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Specific street details or landmarks (e.g., JK Gaji, Nacabs Road, opposite blue gate)"
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_terms_accepted = models.BooleanField(
        default=False,
        help_text="Designates whether the user explicitly ticked the agreement checkbox."
    )

    date_joined = models.DateTimeField(auto_now_add=True)
    terms_accepted_at = models.DateTimeField(
        null=True, 
        blank=True, 
        help_text="Timestamp of when the user explicitly agreed to the Terms of Service."
    )

    token_version = models.IntegerField(default=1)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    def save(self, *args, **kwargs):
        """
        Revoke every issued JWT automatically when the role or password changes,
        regardless of which code path made the change (admin panel, onboarding,
        shell, API). Hash upgrades performed transparently by check_password()
        (where _password is None) do not revoke sessions.
        """
        if self.pk and not self._state.adding:
            previous = (
                type(self).objects.filter(pk=self.pk)
                .values('role', 'password', 'token_version')
                .first()
            )
            if previous:
                role_changed = previous['role'] != self.role
                password_changed = (
                    previous['password'] != self.password
                    and getattr(self, '_password', None) is not None
                )
                if (role_changed or password_changed) and self.token_version == previous['token_version']:
                    self.token_version = previous['token_version'] + 1
                    update_fields = kwargs.get('update_fields')
                    if update_fields is not None and 'token_version' not in update_fields:
                        kwargs['update_fields'] = list(update_fields) + ['token_version']
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class CustomerReview(models.Model):
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=255)
    comment = models.TextField()
    is_approved = models.BooleanField(default=False)  # For moderation before displaying on homepage
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Review by {self.name} ({self.role})"

