from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from .models import MerchantProfile, StorefrontConfig
from accounts.utils import generate_alphanumeric_id
from wallet.models import VendorWallet


# =========================================================================
# COMMERCIAL MERCHANT IDENTITY SIGNAL RECEIVER
# =========================================================================
@receiver(pre_save, sender=MerchantProfile)
def auto_generate_merchant_id(sender, instance, **kwargs):
    """
    Guarantees unique alphanumeric tracking keys with a dedicated 'VEN-' 
    prefix are pre-filled prior to inserting vendor/business rows.
    """
    if not instance.id:
        while True:
            potential_id = f"VEN-{generate_alphanumeric_id(8)}"
            if not sender.objects.filter(id=potential_id).exists():
                instance.id = potential_id
                break

            
@receiver(post_save, sender=MerchantProfile)
def manage_storefront_configuration_generation(sender, instance, created, **kwargs):
    """
    Listens to MerchantProfile lifecycle triggers. 
    Compiles a StorefrontConfig profile for all registered merchant entities upon creation.
    """
    # get_or_create ensures a storefront config exists safely for any business type
    StorefrontConfig.objects.get_or_create(profile=instance)


@receiver(post_save, sender=MerchantProfile)
def create_vendor_wallet(sender, instance, created, **kwargs):
    """
    Creates a VendorWallet automatically when a MerchantProfile is created.
    """
    if created:
        VendorWallet.objects.create(profile=instance)