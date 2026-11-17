from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from .models import StaffProfile, AgentProfile



@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_staff_profile(sender, instance, created, **kwargs):
    if created and instance.role == 'staff':
        StaffProfile.objects.create(user=instance)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_agent_profile(sender, instance, created, **kwargs):
    if created and instance.role == 'agent':
        AgentProfile.objects.create(user=instance)