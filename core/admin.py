# admin.py
from django.contrib import admin, messages
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.urls import reverse
from .models import PlatformSetting, DeliveryConfig


class SingletonModelAdmin(admin.ModelAdmin):
    """
    Base admin class for singleton configuration models (pk=1).
    Redirects /add/ directly to the singleton record and prevents extra rows or deletion.
    """

    def has_add_permission(self, request):
        return not self.model.objects.filter(pk=1).exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj, _ = self.model.objects.get_or_create(pk=1)
        change_url = reverse(
            f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_change",
            args=(obj.pk,),
        )
        return HttpResponseRedirect(change_url)


@admin.register(PlatformSetting)
class PlatformSettingAdmin(SingletonModelAdmin):
    list_display = ('__str__', 'referral_order_threshold', 'referral_bonus_amount', 'updated_at')
    readonly_fields = ('updated_at',)

    fieldsets = (
        ('Referral Thresholds & Bonuses', {
            'fields': ('referral_order_threshold', 'referral_bonus_amount')
        }),
        ('Metadata', {
            'fields': ('updated_at',)
        }),
    )

    def save_model(self, request, obj, form, change):
        obj.pk = 1
        super().save_model(request, obj, form, change)
        cache.delete('platform_settings_cache')
        self.message_user(
            request,
            "Platform settings saved and cache invalidated.",
            level=messages.SUCCESS,
        )


@admin.register(DeliveryConfig)
class DeliveryConfigAdmin(SingletonModelAdmin):
    list_display = ('__str__', 'full_time_rider_earning', 'casual_platform_percentage', 'updated_at')
    readonly_fields = ('updated_at',)

    fieldsets = (
        ('Full-Time Rider Economics', {
            'fields': ('full_time_rider_earning',)
        }),
        ('Casual / Part-Time Rider Economics', {
            'fields': ('casual_platform_percentage',)
        }),
        ('Metadata', {
            'fields': ('updated_at',)
        }),
    )

    def save_model(self, request, obj, form, change):
        obj.pk = 1
        super().save_model(request, obj, form, change)
        self.message_user(
            request,
            "Delivery configuration updated.",
            level=messages.SUCCESS,
        )