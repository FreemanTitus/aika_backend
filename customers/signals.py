from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from .models import IndividualProfile
from accounts.utils import generate_alphanumeric_id


# =========================================================================
# 1. INDIVIDUAL CONSUMER IDENTITY SIGNAL RECEIVER
# =========================================================================
@receiver(pre_save, sender=IndividualProfile)
def auto_generate_customer_id(sender, instance, **kwargs):
    """
    Guarantees unique alphanumeric tracking keys with a dedicated 'CUST-' 
    prefix are pre-filled prior to inserting customer rows.
    """
    if not instance.id:
        while True:
            potential_id = f"CUST-{generate_alphanumeric_id(8)}"
            if not sender.objects.filter(id=potential_id).exists():
                instance.id = potential_id
                break