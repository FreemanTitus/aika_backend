# admin.py
from datetime import timedelta
from django.contrib import admin, messages
from django.utils import timezone
from .models import (
    SubscriptionPlan,
    VendorSubscription,
    SubscriptionInvoice,
    MarketplaceTransaction,
)


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'slug',
        'price',
        'orders_limit_display',
        'products_limit_display',
        'has_priority_dispatch',
        'has_featured_placement',
        'has_advanced_analytics',
        'is_available',
        'created_at',
    )
    list_filter = (
        'is_available',
        'has_priority_dispatch',
        'has_featured_placement',
        'has_advanced_analytics',
        'created_at',
    )
    list_editable = (
        'price',
        'has_priority_dispatch',
        'has_featured_placement',
        'has_advanced_analytics',
        'is_available',
    )
    search_fields = ('name', 'slug', 'description', 'features_raw')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('created_at', 'parsed_features_preview')
    ordering = ('price', 'name')
    actions = ['make_available', 'make_unavailable']

    fieldsets = (
        ('Plan Identity & Pricing', {
            'fields': ('name', 'slug', 'price', 'description', 'is_available')
        }),
        ('Quantitative Limits (Leave blank for Unlimited)', {
            'fields': (('max_monthly_orders', 'max_products'),)
        }),
        ('Feature Perks & Flags', {
            'fields': (
                ('has_priority_dispatch', 'has_featured_placement', 'has_advanced_analytics'),
                'features_raw',
                'parsed_features_preview',
            )
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )

    @admin.display(description='Monthly Orders')
    def orders_limit_display(self, obj):
        return "Unlimited" if obj.max_monthly_orders is None else f"{obj.max_monthly_orders:,}"

    @admin.display(description='Max Products')
    def products_limit_display(self, obj):
        return "Unlimited" if obj.max_products is None else f"{obj.max_products:,}"

    @admin.display(description='Parsed Features Preview')
    def parsed_features_preview(self, obj):
        items = obj.features_list
        return " • ".join(items) if items else "No features parsed yet"

    @admin.action(description="Mark selected plans as available")
    def make_available(self, request, queryset):
        updated = queryset.update(is_available=True)
        self.message_user(request, f"{updated} plan(s) made available.", level=messages.SUCCESS)

    @admin.action(description="Mark selected plans as unavailable")
    def make_unavailable(self, request, queryset):
        updated = queryset.update(is_available=False)
        self.message_user(request, f"{updated} plan(s) marked unavailable.", level=messages.WARNING)


@admin.register(VendorSubscription)
class VendorSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        'vendor_profile',
        'plan',
        'is_active',
        'has_valid_access_display',
        'can_accept_orders_display',
        'cycle_usage_display',
        'subscription_expires_at',
    )
    list_filter = ('is_active', 'plan', 'subscription_expires_at')
    list_editable = ('is_active',)
    search_fields = (
        'vendor_profile__company_name',
        'vendor_profile__id',
        'vendor_profile__user__email',
        'plan__name',
    )
    readonly_fields = (
        'has_valid_access_display',
        'can_accept_orders_display',
        'can_add_product_display',
        'cycle_usage_display',
        'billing_cycle_starts',
    )
    autocomplete_fields = ('vendor_profile', 'plan')
    date_hierarchy = 'subscription_expires_at'
    ordering = ('-subscription_expires_at',)
    actions = ['extend_30_days', 'activate_subscriptions', 'deactivate_subscriptions']

    fieldsets = (
        ('Vendor & Active Tier', {
            'fields': ('vendor_profile', 'plan', 'is_active', 'subscription_expires_at')
        }),
        ('Live Entitlement & Quota Diagnostics', {
            'fields': (
                ('has_valid_access_display', 'can_accept_orders_display', 'can_add_product_display'),
                ('billing_cycle_starts', 'cycle_usage_display'),
            )
        }),
    )

    @admin.display(description='Valid Access', boolean=True)
    def has_valid_access_display(self, obj):
        return obj.has_valid_access

    @admin.display(description='Can Accept Orders', boolean=True)
    def can_accept_orders_display(self, obj):
        return obj.can_accept_orders

    @admin.display(description='Can Add Products', boolean=True)
    def can_add_product_display(self, obj):
        return obj.can_add_product

    @admin.display(description='Cycle Orders (Used / Limit)')
    def cycle_usage_display(self, obj):
        used = obj.get_orders_this_cycle_count()
        if not obj.plan or obj.plan.is_unlimited_orders:
            return f"{used} / Unlimited"
        return f"{used} / {obj.plan.max_monthly_orders}"

    @admin.action(description="Activate and extend selected subscriptions by 30 days")
    def extend_30_days(self, request, queryset):
        now = timezone.now()
        count = 0
        for sub in queryset:
            base = sub.subscription_expires_at if (sub.subscription_expires_at and sub.subscription_expires_at > now) else now
            sub.subscription_expires_at = base + timedelta(days=30)
            sub.is_active = True
            sub.save(update_fields=['subscription_expires_at', 'is_active'])
            count += 1
        self.message_user(request, f"Extended {count} subscription(s) by 30 days.", level=messages.SUCCESS)

    @admin.action(description="Activate selected subscriptions")
    def activate_subscriptions(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} subscription(s) activated.", level=messages.SUCCESS)

    @admin.action(description="Deactivate selected subscriptions")
    def deactivate_subscriptions(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} subscription(s) deactivated.", level=messages.WARNING)


@admin.register(SubscriptionInvoice)
class SubscriptionInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'reference_id',
        'vendor_profile',
        'plan',
        'amount',
        'status',
        'payment_date',
    )
    list_filter = ('status', 'plan', 'payment_date')
    search_fields = (
        'reference_id',
        'vendor_profile__company_name',
        'vendor_profile__user__email',
        'plan__name',
    )
    readonly_fields = ('payment_date',)
    autocomplete_fields = ('vendor_profile', 'plan')
    date_hierarchy = 'payment_date'
    ordering = ('-payment_date',)
    actions = ['mark_successful', 'mark_failed']

    @admin.action(description="Mark selected invoices as Successful")
    def mark_successful(self, request, queryset):
        updated = queryset.update(status=SubscriptionInvoice.PaymentStatus.SUCCESSFUL)
        self.message_user(request, f"{updated} invoice(s) marked as Successful.", level=messages.SUCCESS)

    @admin.action(description="Mark selected invoices as Failed")
    def mark_failed(self, request, queryset):
        updated = queryset.update(status=SubscriptionInvoice.PaymentStatus.FAILED)
        self.message_user(request, f"{updated} invoice(s) marked as Failed.", level=messages.WARNING)


@admin.register(MarketplaceTransaction)
class MarketplaceTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'order_id',
        'vendor_profile',
        'total_basket_value',
        'platform_fee_collected',
        'vendor_payout_share',
        'paystack_reference',
        'timestamp',
    )
    list_filter = ('timestamp', 'vendor_profile')
    search_fields = (
        'order_id',
        'paystack_reference',
        'vendor_profile__company_name',
        'vendor_profile__user__email',
    )
    readonly_fields = (
        'order_id',
        'vendor_profile',
        'total_basket_value',
        'platform_fee_collected',
        'vendor_payout_share',
        'paystack_reference',
        'timestamp',
    )
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)

    # Keep settled marketplace payout records immutable in Django Admin
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False