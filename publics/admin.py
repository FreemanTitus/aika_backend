# admin.py
from django.contrib import admin, messages
from .models import NewsletterSubscriber, NewsletterBroadcast


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ('email', 'is_active', 'subscribed_at')
    list_filter = ('is_active', 'subscribed_at')
    list_editable = ('is_active',)
    search_fields = ('email',)
    readonly_fields = ('subscribed_at',)
    date_hierarchy = 'subscribed_at'
    ordering = ('-subscribed_at',)
    actions = ['activate_subscribers', 'deactivate_subscribers']

    def save_model(self, request, obj, form, change):
        if obj.email:
            obj.email = obj.email.strip().lower()
        super().save_model(request, obj, form, change)

    @admin.action(description="Activate selected subscribers")
    def activate_subscribers(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} subscriber(s) activated.", level=messages.SUCCESS)

    @admin.action(description="Unsubscribe / deactivate selected subscribers")
    def deactivate_subscribers(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} subscriber(s) deactivated.", level=messages.WARNING)


@admin.register(NewsletterBroadcast)
class NewsletterBroadcastAdmin(admin.ModelAdmin):
    list_display = (
        'subject',
        'template_type',
        'recipient_count',
        'sent_by',
        'created_at',
    )
    list_filter = ('template_type', 'created_at')
    search_fields = ('subject', 'content', 'sent_by__email')
    readonly_fields = ('sent_by', 'created_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    fieldsets = (
        ('Broadcast Details', {
            'fields': ('subject', 'template_type', 'content')
        }),
        ('Delivery & Metadata', {
            'fields': ('recipient_count', 'sent_by', 'created_at')
        }),
    )

    def save_model(self, request, obj, form, change):
        if not obj.sent_by:
            obj.sent_by = request.user
        # Populate recipient_count from active subscribers if left at 0 on creation
        if not change and obj.recipient_count == 0:
            obj.recipient_count = NewsletterSubscriber.objects.filter(is_active=True).count()
        super().save_model(request, obj, form, change)