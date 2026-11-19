# admin.py
from django.contrib import admin, messages
from django.utils.html import format_html
from .models import (
    RiderProfile,
    RiderReview,
    FCMDeviceToken,
    RiderTelemetryLog,
    RiderLocation,
    RiderPaymentAccount,
    RiderWallet,
    RiderTransactionLedger,
)


# ─────────────────────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────────────────────

class RiderPaymentAccountInline(admin.StackedInline):
    model = RiderPaymentAccount
    extra = 0
    can_delete = False
    fields = ('bank_name', 'bank_code', 'account_number', 'account_name', 'is_verified')


class RiderWalletInline(admin.StackedInline):
    model = RiderWallet
    extra = 0
    can_delete = False
    fields = ('balance', 'total_earned', 'updated_at')
    readonly_fields = ('updated_at',)


class RiderTransactionLedgerInline(admin.TabularInline):
    model = RiderTransactionLedger
    extra = 0
    can_delete = False
    fields = ('created_at', 'transaction_type', 'amount', 'reference', 'description')
    readonly_fields = ('created_at', 'transaction_type', 'amount', 'reference', 'description')
    ordering = ('-created_at',)

    def has_add_permission(self, request, obj=None):
        return False


# ─────────────────────────────────────────────────────────────
# Core Rider & KYC Admins
# ─────────────────────────────────────────────────────────────

@admin.register(RiderProfile)
class RiderProfileAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'passport_thumb',
        'full_name_display',
        'user_email',
        'bike_plate_number',
        'employment_type',
        'status',
        'device_type',
        'rating',
        'review_count',
        'is_id_verified',
        'is_vetted',
        'is_active',
    )
    list_filter = (
        'status',
        'employment_type',
        'device_type',
        'payout_schedule',
        'payout_method',
        'is_id_verified',
        'is_vetted',
        'is_active',
        'guarantor_employment_status',
    )
    list_editable = ('status', 'is_id_verified', 'is_vetted', 'is_active')
    search_fields = (
        'id',
        'first_name',
        'last_name',
        'bike_plate_number',
        'id_number',
        'guarantor_name',
        'guarantor_phone',
        'user__email',
        'user__phone_number',
    )
    readonly_fields = (
        'id',
        'rating',
        'review_count',
        'id_document_preview',
        'passport_preview',
        'guarantor_passport_preview',
    )
    autocomplete_fields = ('user',)
    inlines = [RiderPaymentAccountInline, RiderWalletInline, RiderTransactionLedgerInline]
    ordering = ('first_name', 'last_name')
    actions = [
        'vet_and_verify_riders',
        'mark_available',
        'mark_offline',
        'activate_riders',
        'deactivate_riders',
        'recalculate_rider_ratings',
    ]

    fieldsets = (
        ('Rider Identity & Account', {
            'fields': ('id', 'user', ('first_name', 'last_name'), 'bike_plate_number')
        }),
        ('Operational Status & Dispatch Mode', {
            'fields': (
                ('status', 'is_active', 'is_vetted'),
                ('employment_type', 'device_type'),
                ('payout_schedule', 'payout_method'),
                ('rating', 'review_count'),
            )
        }),
        ('Rider KYC & Identity Documents', {
            'fields': (
                ('is_id_verified', 'id_type', 'id_number'),
                ('id_document_image', 'id_document_preview'),
                ('passport_photograph', 'passport_preview'),
            )
        }),
        ('Guarantor KYC & Employment Details', {
            'fields': (
                ('guarantor_name', 'guarantor_phone', 'guarantor_relationship'),
                'guarantor_address',
                ('guarantor_employment_status', 'guarantor_occupation'),
                'guarantor_workplace_address',
                ('guarantor_passport_photograph', 'guarantor_passport_preview'),
            )
        }),
    )

    @admin.display(description='Full Name', ordering='first_name')
    def full_name_display(self, obj):
        return obj.full_name

    @admin.display(description='Email', ordering='user__email')
    def user_email(self, obj):
        return obj.user.email if obj.user else "—"

    @admin.display(description='Photo')
    def passport_thumb(self, obj):
        if obj.passport_photograph:
            return format_html(
                '<img src="{0}" style="height:38px; width:38px; object-fit:cover; border-radius:9999px;" />',
                obj.passport_photograph.url,
            )
        return "—"

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
        return "No passport photo uploaded"

    @admin.display(description='Guarantor Photo Preview')
    def guarantor_passport_preview(self, obj):
        if obj.guarantor_passport_photograph:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="max-height:140px; border-radius:8px; border:1px solid #ddd;" /></a>',
                obj.guarantor_passport_photograph.url,
            )
        return "No guarantor photo uploaded"

    @admin.action(description="Vet & verify ID for selected riders")
    def vet_and_verify_riders(self, request, queryset):
        updated = queryset.update(is_vetted=True, is_id_verified=True)
        self.message_user(request, f"{updated} rider(s) marked as vetted and ID verified.", level=messages.SUCCESS)

    @admin.action(description="Mark selected riders as Available")
    def mark_available(self, request, queryset):
        updated = queryset.update(status=RiderProfile.Status.AVAILABLE)
        self.message_user(request, f"{updated} rider(s) marked Available.", level=messages.SUCCESS)

    @admin.action(description="Mark selected riders as Offline")
    def mark_offline(self, request, queryset):
        updated = queryset.update(status=RiderProfile.Status.OFFLINE)
        self.message_user(request, f"{updated} rider(s) marked Offline.", level=messages.WARNING)

    @admin.action(description="Activate selected rider profiles")
    def activate_riders(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} rider profile(s) activated.", level=messages.SUCCESS)

    @admin.action(description="Deactivate selected rider profiles")
    def deactivate_riders(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} rider profile(s) deactivated.", level=messages.WARNING)

    @admin.action(description="Recalculate ratings for selected riders")
    def recalculate_rider_ratings(self, request, queryset):
        count = 0
        for rider in queryset:
            rider.recalculate_ratings()
            count += 1
        self.message_user(request, f"Ratings recalculated for {count} rider(s).", level=messages.INFO)


@admin.register(RiderReview)
class RiderReviewAdmin(admin.ModelAdmin):
    list_display = ('rider', 'customer', 'rating', 'short_comment', 'created_at', 'updated_at')
    list_filter = ('rating', 'created_at')
    search_fields = ('rider__first_name', 'rider__last_name', 'rider__bike_plate_number', 'customer__email', 'comment')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('rider', 'customer')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    @admin.display(description='Comment')
    def short_comment(self, obj):
        if not obj.comment:
            return "—"
        return (obj.comment[:60] + '...') if len(obj.comment) > 60 else obj.comment

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:
            obj.rider.recalculate_ratings()

    def delete_model(self, request, obj):
        rider = obj.rider
        super().delete_model(request, obj)
        rider.recalculate_ratings()

    def delete_queryset(self, request, queryset):
        riders = set(queryset.values_list('rider_id', flat=True))
        super().delete_queryset(request, queryset)
        for rider in RiderProfile.objects.filter(pk__in=riders):
            rider.recalculate_ratings()


# ─────────────────────────────────────────────────────────────
# Telemetry, Location & Push Tokens Admins
# ─────────────────────────────────────────────────────────────

@admin.register(RiderLocation)
class RiderLocationAdmin(admin.ModelAdmin):
    list_display = ('rider', 'latitude', 'longitude', 'updated_at')
    search_fields = ('rider__first_name', 'rider__last_name', 'rider__bike_plate_number', 'rider__user__email')
    readonly_fields = ('updated_at',)
    autocomplete_fields = ('rider',)
    ordering = ('-updated_at',)


@admin.register(RiderTelemetryLog)
class RiderTelemetryLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'order', 'rider_email', 'latitude', 'longitude')
    list_filter = ('timestamp',)
    search_fields = ('order__order_reference', 'rider__email', 'rider__phone_number')
    readonly_fields = ('order', 'rider', 'latitude', 'longitude', 'timestamp')
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)

    @admin.display(description='Rider', ordering='rider__email')
    def rider_email(self, obj):
        return obj.rider.email

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(FCMDeviceToken)
class FCMDeviceTokenAdmin(admin.ModelAdmin):
    list_display = ('user_email', 'token_preview', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__email', 'user__phone_number', 'token')
    readonly_fields = ('created_at',)
    autocomplete_fields = ('user',)
    ordering = ('-created_at',)

    @admin.display(description='User', ordering='user__email')
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description='Token')
    def token_preview(self, obj):
        return (obj.token[:36] + '...') if len(obj.token) > 36 else obj.token


# ─────────────────────────────────────────────────────────────
# Payout Accounts, Wallets & Ledger Admins
# ─────────────────────────────────────────────────────────────

@admin.register(RiderPaymentAccount)
class RiderPaymentAccountAdmin(admin.ModelAdmin):
    list_display = ('rider', 'bank_name', 'bank_code', 'account_number', 'account_name', 'is_verified')
    list_filter = ('is_verified', 'bank_name')
    list_editable = ('is_verified',)
    search_fields = (
        'rider__first_name',
        'rider__last_name',
        'rider__bike_plate_number',
        'rider__user__email',
        'bank_name',
        'account_number',
        'account_name',
    )
    autocomplete_fields = ('rider',)
    ordering = ('rider__first_name', 'rider__last_name')
    actions = ['mark_accounts_verified', 'mark_accounts_unverified']

    @admin.action(description="Mark selected payout accounts as verified")
    def mark_accounts_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"{updated} payout account(s) verified.", level=messages.SUCCESS)

    @admin.action(description="Revoke verification for selected payout accounts")
    def mark_accounts_unverified(self, request, queryset):
        updated = queryset.update(is_verified=False)
        self.message_user(request, f"{updated} payout account(s) marked unverified.", level=messages.WARNING)


@admin.register(RiderWallet)
class RiderWalletAdmin(admin.ModelAdmin):
    list_display = ('rider', 'balance', 'total_earned', 'updated_at')
    search_fields = ('rider__first_name', 'rider__last_name', 'rider__bike_plate_number', 'rider__user__email')
    readonly_fields = ('updated_at',)
    autocomplete_fields = ('rider',)
    # inlines = [RiderTransactionLedgerInline]
    ordering = ('-updated_at',)


@admin.register(RiderTransactionLedger)
class RiderTransactionLedgerAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'reference', 'rider', 'transaction_type', 'amount', 'description')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('reference', 'rider__first_name', 'rider__last_name', 'rider__user__email', 'description')
    readonly_fields = ('rider', 'amount', 'transaction_type', 'description', 'reference', 'created_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    # Keep rider financial ledger entries immutable in Django Admin
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False