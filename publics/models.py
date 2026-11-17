from django.db import models
from accounts.models import CustomUser


class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    subscribed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email

class NewsletterBroadcast(models.Model):
    class TemplateType(models.TextChoices):
        STANDARD = 'standard', 'Standard Announcement'
        PROMO = 'promo', 'Promotional / Offer'
        UPDATE = 'update', 'Product & Feature Update'

    subject = models.CharField(max_length=255)
    content = models.TextField()
    template_type = models.CharField(
        max_length=20, 
        choices=TemplateType.choices, 
        default=TemplateType.STANDARD
    )
    sent_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    recipient_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.subject

