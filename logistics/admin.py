# admin.py
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from .models import LogisticsPricingConfig


@admin.register(LogisticsPricingConfig)
class LogisticsPricingConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'base_fare', 'per_kilometer_rate')

    fieldsets = (
        ('Delivery Fare Rates', {
            'fields': ('base_fare', 'per_kilometer_rate')
        }),
    )

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

    def save_model(self, request, obj, form, change):
        obj.pk = 1
        super().save_model(request, obj, form, change)
        self.message_user(
            request,
            "Logistics pricing configuration saved.",
            level=messages.SUCCESS,
        )