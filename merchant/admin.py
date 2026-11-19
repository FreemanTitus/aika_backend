# admin.py
from django.contrib import admin, messages
from django.utils.html import format_html
from .models import (
    MerchantProfile,
    VendorShippingPreference,
    MerchantReview,
    StorefrontConfig,
)


# ─────────────────────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────────────────────

class VendorShippingPreferenceInline(admin.StackedInline):
    model = VendorShippingPreference
    extra = 0
    can_delete = False
    fields = ('fulfillment_type', 'allow_nationwide', 'self_delivery_fee')


class StorefrontConfigInline(admin.StackedInline):
    model = StorefrontConfig
    extra = 0
    fields = (
        'store_slug',
        ('hero_image', 'hero_preview'),
        'tagline',
        'brand_bio',
        ('instagram_handle', 'twitter_handle'),
        'created_at',
        'updated_at',
    )
    readonly_fields = ('hero_preview', 'created_at', 'updated_at')

    @admin.display(description='Hero Banner Preview')
    def hero_preview(self, obj):
        if obj.hero_image:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height:110px; border-radius:8px; border:1px solid #ddd;" /></a>',
                obj.hero_image.url,
            )
        return "No hero banner uploaded"


# ─────────────────────────────────────────────────────────────
# Model Admins
# ─────────────────────────────────────────────────────────────

@admin.register(MerchantProfile)
class MerchantProfileAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'company_name',
        'user_email',
        'business_type',
        'ecommerce_sub_category',
        'rating',
        'review_count',
        'is_currently_open_display',
        'is_approved',
        'is_active_store',
        'assigned_agent',
    )
    list_filter = (
        'business_type',
        'ecommerce_sub_category',
        'is_approved',
        'is_active_store',
        'assigned_agent',
    )
    list_editable = ('is_approved', 'is_active_store')
    search_fields = (
        'id',
        'company_name',
        'user__email',
        'user__phone_number',
        'paystack_subaccount_code',
        'assigned_agent__email',
    )
    readonly_fields = (
        'id',
        'rating',
        'review_count',
        'count_5_stars',
        'count_4_stars',
        'count_3_stars',
        'count_2_stars',
        'count_1_stars',
        'is_currently_open_display',
    )
    autocomplete_fields = ('user', 'assigned_agent')
    inlines = [VendorShippingPreferenceInline, StorefrontConfigInline]
    ordering = ('company_name',)
    actions = [
        'approve_merchants',
        'unapprove_merchants',
        'activate_stores',
        'deactivate_stores',
        'recalculate_merchant_ratings',
    ]

    fieldsets = (
        ('Merchant Identity & Account', {
            'fields': ('id', 'user', 'company_name', ('business_type', 'ecommerce_sub_category'), 'assigned_agent')
        }),
        ('Store Status & Operating Hours', {
            'fields': (
                ('is_approved', 'is_active_store', 'is_currently_open_display'),
                ('opening_time', 'closing_time'),
            )
        }),
        ('Paystack Split Settlement', {
            'fields': ('paystack_subaccount_code',)
        }),
        ('Ratings & Review Breakdown', {
            'fields': (
                ('rating', 'review_count'),
                ('count_5_stars', 'count_4_stars', 'count_3_stars', 'count_2_stars', 'count_1_stars'),
            )
        }),
    )

    @admin.display(description='User Email', ordering='user__email')
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description='Open Now', boolean=True)
    def is_currently_open_display(self, obj):
        return obj.is_currently_open

    def save_model(self, request, obj, form, change):
        # Iterate and save each instance so MerchantProfile.save() triggers Paystack subaccount creation
        super().save_model(request, obj, form, change)

    @admin.action(description="Approve selected merchant profiles")
    def approve_merchants(self, request, queryset):
        count = 0
        for merchant in queryset:
            merchant.is_approved = True
            merchant.save()
            count += 1
        self.message_user(request, f"{count} merchant profile(s) approved.", level=messages.SUCCESS)

    @admin.action(description="Revoke approval for selected merchant profiles")
    def unapprove_merchants(self, request, queryset):
        updated = queryset.update(is_approved=False)
        self.message_user(request, f"{updated} merchant profile(s) set to unapproved.", level=messages.WARNING)

    @admin.action(description="Activate selected stores in marketplace")
    def activate_stores(self, request, queryset):
        updated = queryset.update(is_active_store=True)
        self.message_user(request, f"{updated} store(s) activated.", level=messages.SUCCESS)

    @admin.action(description="Deactivate selected stores in marketplace")
    def deactivate_stores(self, request, queryset):
        updated = queryset.update(is_active_store=False)
        self.message_user(request, f"{updated} store(s) deactivated.", level=messages.WARNING)

    @admin.action(description="Recalculate ratings for selected merchants")
    def recalculate_merchant_ratings(self, request, queryset):
        count = 0
        for merchant in queryset:
            merchant.recalculate_ratings()
            count += 1
        self.message_user(request, f"Ratings recalculated for {count} merchant(s).", level=messages.INFO)


@admin.register(VendorShippingPreference)
class VendorShippingPreferenceAdmin(admin.ModelAdmin):
    list_display = ('vendor', 'fulfillment_type', 'allow_nationwide', 'self_delivery_fee')
    list_filter = ('fulfillment_type', 'allow_nationwide')
    list_editable = ('fulfillment_type', 'allow_nationwide', 'self_delivery_fee')
    search_fields = ('vendor__company_name', 'vendor__id', 'vendor__user__email')
    autocomplete_fields = ('vendor',)
    ordering = ('vendor__company_name',)


@admin.register(MerchantReview)
class MerchantReviewAdmin(admin.ModelAdmin):
    list_display = ('merchant', 'customer', 'rating', 'short_comment', 'created_at', 'updated_at')
    list_filter = ('rating', 'created_at')
    search_fields = ('merchant__company_name', 'customer__email', 'comment')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('merchant', 'customer')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    @admin.display(description='Comment')
    def short_comment(self, obj):
        if not obj.comment:
            return "—"
        return (obj.comment[:60] + '...') if len(obj.comment) > 60 else obj.comment

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Note: model.save() only recalculates on creation; recalculate on edits too
        if change:
            obj.merchant.recalculate_ratings()

    def delete_model(self, request, obj):
        merchant = obj.merchant
        super().delete_model(request, obj)
        merchant.recalculate_ratings()

    def delete_queryset(self, request, queryset):
        merchants = set(queryset.values_list('merchant_id', flat=True))
        super().delete_queryset(request, queryset)
        for merchant in MerchantProfile.objects.filter(pk__in=merchants):
            merchant.recalculate_ratings()


@admin.register(StorefrontConfig)
class StorefrontConfigAdmin(admin.ModelAdmin):
    list_display = (
        'profile',
        'store_slug',
        'hero_preview',
        'instagram_handle',
        'twitter_handle',
        'updated_at',
    )
    search_fields = ('profile__company_name', 'store_slug', 'tagline', 'instagram_handle', 'twitter_handle')
    readonly_fields = ('created_at', 'updated_at', 'hero_preview')
    autocomplete_fields = ('profile',)
    prepopulated_fields = {'store_slug': ('tagline',)}
    ordering = ('profile__company_name',)

    fieldsets = (
        ('Merchant & Storefront URL', {
            'fields': ('profile', 'store_slug')
        }),
        ('Brand Identity & Hero Banner', {
            'fields': (('hero_image', 'hero_preview'), 'tagline', 'brand_bio')
        }),
        ('Social Links', {
            'fields': ('instagram_handle', 'twitter_handle')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @admin.display(description='Hero Preview')
    def hero_preview(self, obj):
        if obj.hero_image:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height:110px; border-radius:8px; border:1px solid #ddd;" /></a>',
                obj.hero_image.url,
            )
        return "No hero banner uploaded"