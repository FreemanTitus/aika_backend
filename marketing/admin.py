# admin.py
from django.contrib import admin, messages
from django.utils import timezone
from .models import SystemBroadcast, UserNotification, PromoCode


@admin.register(SystemBroadcast)
class SystemBroadcastAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'target_audience',
        'sent_by',
        'is_read',
        'created_at',
    )
    list_filter = ('target_audience', 'is_read', 'created_at')
    list_editable = ('is_read',)
    search_fields = ('title', 'message', 'sent_by__email')
    readonly_fields = ('sent_by', 'created_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    fieldsets = (
        ('Broadcast Content', {
            'fields': ('title', 'message', 'target_audience')
        }),
        ('Delivery & Metadata', {
            'fields': ('sent_by', 'is_read', 'created_at')
        }),
    )

    def save_model(self, request, obj, form, change):
        if not obj.sent_by:
            obj.sent_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(UserNotification)
class UserNotificationAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'user',
        'notification_type',
        'is_read',
        'created_at',
    )
    list_filter = ('notification_type', 'is_read', 'created_at')
    list_editable = ('is_read',)
    search_fields = ('title', 'message', 'user__email', 'user__phone_number')
    readonly_fields = ('created_at',)
    autocomplete_fields = ('user',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    actions = ['mark_as_read', 'mark_as_unread']

    @admin.action(description="Mark selected notifications as read")
    def mark_as_read(self, request, queryset):
        updated = queryset.update(is_read=True)
        self.message_user(request, f"{updated} notification(s) marked as read.", level=messages.SUCCESS)

    @admin.action(description="Mark selected notifications as unread")
    def mark_as_unread(self, request, queryset):
        updated = queryset.update(is_read=False)
        self.message_user(request, f"{updated} notification(s) marked as unread.", level=messages.INFO)


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = (
        'code',
        'discount_type',
        'value',
        'min_order_amount',
        'usage_summary',
        'is_active',
        'is_expired',
        'expires_at',
        'created_at',
    )
    list_filter = ('discount_type', 'is_active', 'expires_at', 'created_at')
    list_editable = ('is_active',)
    search_fields = ('code',)
    readonly_fields = ('times_used', 'created_at')
    date_hierarchy = 'expires_at'
    ordering = ('-created_at',)
    actions = ['activate_promos', 'deactivate_promos']

    fieldsets = (
        ('Promo Identity & Discount', {
            'fields': ('code', ('discount_type', 'value'), 'min_order_amount')
        }),
        ('Usage Limits & Schedule', {
            'fields': ('is_active', ('times_used', 'usage_limit'), 'expires_at', 'created_at')
        }),
    )

    @admin.display(description='Usage')
    def usage_summary(self, obj):
        return f"{obj.times_used} / {obj.usage_limit}"

    @admin.display(description='Expired', boolean=True)
    def is_expired(self, obj):
        return obj.expires_at <= timezone.now()

    def save_model(self, request, obj, form, change):
        if obj.code:
            obj.code = obj.code.strip().upper()
        super().save_model(request, obj, form, change)

    @admin.action(description="Activate selected promo codes")
    def activate_promos(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} promo code(s) activated.", level=messages.SUCCESS)

    @admin.action(description="Deactivate selected promo codes")
    def deactivate_promos(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} promo code(s) deactivated.", level=messages.WARNING)