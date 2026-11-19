# admin.py
from django.contrib import admin, messages
from django.utils.html import format_html
from .models import (
    IndividualProfile,
    Referral,
    FrequentlyAskedQuestion,
    CustomerWallet,
    CustomerWalletTransaction,
)


@admin.register(IndividualProfile)
class IndividualProfileAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'full_name_display',
        'user_email',
        'id_type',
        'is_id_verified',
        'referral_code',
        'referred_by',
        'referral_reward_unlocked',
        'created_at',
    )
    list_filter = ('is_id_verified', 'id_type', 'referral_reward_unlocked', 'created_at')
    list_editable = ('is_id_verified',)
    search_fields = (
        'id',
        'first_name',
        'last_name',
        'user__email',
        'user__phone_number',
        'id_number',
        'referral_code',
        'referred_by__email',
    )
    readonly_fields = ('id', 'referral_code', 'created_at', 'id_document_preview', 'passport_preview')
    autocomplete_fields = ('user', 'referred_by')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    actions = ['verify_identities', 'unverify_identities']

    fieldsets = (
        ('Profile Identity', {
            'fields': ('id', 'user', ('first_name', 'last_name'), 'default_delivery_address')
        }),
        ('KYC / Government ID Verification', {
            'fields': (
                'is_id_verified',
                ('id_type', 'id_number'),
                ('id_document_image', 'id_document_preview'),
                ('passport_photograph', 'passport_preview'),
            )
        }),
        ('Referral Tracking', {
            'fields': ('referral_code', 'referred_by', 'referral_reward_unlocked')
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )

    @admin.display(description='Full Name', ordering='first_name')
    def full_name_display(self, obj):
        return obj.full_name

    @admin.display(description='Email', ordering='user__email')
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description='ID Document Preview')
    def id_document_preview(self, obj):
        if obj.id_document_image:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height:140px; border-radius:8px; border:1px solid #ddd;" /></a>',
                obj.id_document_image.url,
            )
        return "No ID document uploaded"

    @admin.display(description='Passport Preview')
    def passport_preview(self, obj):
        if obj.passport_photograph:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height:140px; border-radius:8px; border:1px solid #ddd;" /></a>',
                obj.passport_photograph.url,
            )
        return "No portrait uploaded"

    @admin.action(description="Mark selected customer profiles as ID Verified")
    def verify_identities(self, request, queryset):
        updated = queryset.update(is_id_verified=True)
        self.message_user(request, f"{updated} customer profile(s) verified.", level=messages.SUCCESS)

    @admin.action(description="Revoke ID Verification for selected profiles")
    def unverify_identities(self, request, queryset):
        updated = queryset.update(is_id_verified=False)
        self.message_user(request, f"{updated} customer profile(s) unverified.", level=messages.WARNING)


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = (
        'inviter',
        'invitee',
        'has_completed_qualifying_order',
        'qualifying_order_id',
        'reward_granted',
        'created_at',
        'updated_at',
    )
    list_filter = ('has_completed_qualifying_order', 'reward_granted', 'created_at')
    search_fields = ('inviter__email', 'invitee__email', 'qualifying_order_id')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('inviter', 'invitee')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


@admin.register(FrequentlyAskedQuestion)
class FrequentlyAskedQuestionAdmin(admin.ModelAdmin):
    list_display = ('order', 'question', 'is_active', 'created_at')
    list_display_links = ('question',)
    list_filter = ('is_active', 'created_at')
    list_editable = ('order', 'is_active')
    search_fields = ('question', 'answer')
    readonly_fields = ('created_at',)
    ordering = ('order', '-created_at')


class CustomerWalletTransactionInline(admin.TabularInline):
    model = CustomerWalletTransaction
    extra = 0
    can_delete = False
    fields = ('created_at', 'transaction_type', 'amount', 'reference', 'description')
    readonly_fields = ('created_at', 'transaction_type', 'amount', 'reference', 'description')
    ordering = ('-created_at',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CustomerWallet)
class CustomerWalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance', 'currency', 'is_active', 'updated_at', 'created_at')
    list_filter = ('is_active', 'currency', 'created_at')
    list_editable = ('is_active',)
    search_fields = ('user__email', 'user__phone_number')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('user',)
    inlines = [CustomerWalletTransactionInline]
    ordering = ('-updated_at',)


@admin.register(CustomerWalletTransaction)
class CustomerWalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'wallet_user', 'transaction_type', 'amount', 'reference')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('reference', 'wallet__user__email', 'description')
    readonly_fields = ('wallet', 'amount', 'transaction_type', 'reference', 'description', 'created_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    @admin.display(description='Customer', ordering='wallet__user__email')
    def wallet_user(self, obj):
        return obj.wallet.user.email

    # Keep financial ledger entries immutable in Django Admin
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False