from django.db import models


class LogisticsPricingConfig(models.Model):
    base_fare = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=1000.00, 
        help_text="Base two-way trip delivery fee covering restaurant pickup and customer dropoff."
    )
    per_kilometer_rate = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=150.00
    )