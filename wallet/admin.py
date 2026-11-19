# admin.py
from django.contrib import admin, messages
from django.utils import timezone
from .models import VendorWallet, PaymentSettlementAccount, PayoutLog


class PayoutLogInline(admin.TabularInline):
    model = PayoutLog
    extra = 0
    can_delete = False
    fields = (
        'created_at',
        'reference',
        'amount',
        'status',
        'opay_order_id',
        'balance_restored',
        'failure_reason',
    )
    readonly_fields = (
        'created_at',
        'reference',
        'amount',
        'status',
        'opay_order_id',
        'balance_restored',
        'failure_reason',
    )
    ordering = ('-created_at',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(VendorWallet)
class VendorWalletAdmin(admin.ModelAdmin):
    list_display = (
        'profile',
        'available_balance',
        'escrow_balance',
        'has_pin_set',
        'pin_failed_attempts',
        'pin_locked_until',
        'security_hold_until',
        'updated_at',
    )
    list_filter = ('pin_locked_until', 'security_hold_until', 'created_at', 'updated_at')
    search_fields = (
        'profile__company_name',
        'profile__id',
        'profile__user__email',
        'profile__user__phone_number',
    )
    readonly_fields = ('has_pin_set', 'created_at', 'updated_at')
    autocomplete_fields = ('profile',)
    inlines = [PayoutLogInline]
    ordering = ('-updated_at',)
    actions = ['clear_pin_lock_and_hold']

    fieldsets = (
        ('Vendor Wallet & Balances', {
            'fields': ('profile', ('available_balance', 'escrow_balance'))
        }),
        ('PIN & Security Controls', {
            'fields': (
                'has_pin_set',
                'transfer_pin',
                ('pin_failed_attempts', 'pin_locked_until'),
                'security_hold_until',
            )
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @admin.display(description='PIN Set', boolean=True)
    def has_pin_set(self, obj):
        return bool(obj.transfer_pin)

    @admin.action(description="Clear PIN lockouts & security holds for selected wallets")
    def clear_pin_lock_and_hold(self, request, queryset):
        updated = queryset.update(
            pin_failed_attempts=0,
            pin_locked_until=None,
            security_hold_until=None,
        )
        self.message_user(
            request,
            f"Cleared PIN lockouts and security holds on {updated} wallet(s).",
            level=messages.SUCCESS,
        )


@admin.register(PaymentSettlementAccount)
class PaymentSettlementAccountAdmin(admin.ModelAdmin):
    list_display = (
        'profile',
        'bank_name',
        'bank_code',
        'account_number',
        'account_name',
        'recipient_code',
        'is_verified',
    )
    list_filter = ('is_verified', 'bank_name')
    list_editable = ('is_verified',)
    search_fields = (
        'profile__company_name',
        'profile__id',
        'profile__user__email',
        'bank_name',
        'bank_code',
        'account_number',
        'account_name',
        'recipient_code',
    )
    autocomplete_fields = ('profile',)
    ordering = ('profile__company_name',)
    actions = ['mark_verified', 'mark_unverified']

    @admin.action(description="Mark selected settlement accounts as verified")
    def mark_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"{updated} settlement account(s) verified.", level=messages.SUCCESS)

    @admin.action(description="Mark selected settlement accounts as unverified")
    def mark_unverified(self, request, queryset):
        updated = queryset.update(is_verified=False)
        self.message_user(request, f"{updated} settlement account(s) unverified.", level=messages.WARNING)


@admin.register(PayoutLog)
class PayoutLogAdmin(admin.ModelAdmin):
    list_display = (
        'reference',
        'wallet',
        'amount',
        'status',
        'opay_order_id',
        'balance_restored',
        'failure_reason',
        'created_at',
    )
    list_filter = ('status', 'balance_restored', 'created_at')
    search_fields = (
        'reference',
        'opay_order_id',
        'failure_reason',
        'wallet__profile__company_name',
        'wallet__profile__user__email',
    )
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('wallet',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    actions = ['mark_processing', 'mark_success', 'mark_failed_and_restore_balance']

    fieldsets = (
        ('Payout Reference & Wallet', {
            'fields': ('reference', 'wallet', 'amount')
        }),
        ('OPay Execution & Status', {
            'fields': ('status', 'opay_order_id', 'balance_restored', 'failure_reason')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @admin.action(description="Mark selected payouts as Processing with OPay")
    def mark_processing(self, request, queryset):
        updated = queryset.exclude(status=PayoutLog.PayoutStatus.SUCCESS).update(
            status=PayoutLog.PayoutStatus.PROCESSING,
            updated_at=timezone.now(),
        )
        self.message_user(request, f"{updated} payout(s) marked as Processing.", level=messages.INFO)

    @admin.action(description="Mark selected payouts as Settled Successfully")
    def mark_success(self, request, queryset):
        updated = queryset.update(
            status=PayoutLog.PayoutStatus.SUCCESS,
            failure_reason="",
            updated_at=timezone.now(),
        )
        self.message_user(request, f"{updated} payout(s) marked as Settled Successfully.", level=messages.SUCCESS)

    @admin.action(description="Mark selected payouts as Failed & restore unrefunded balances")
    def mark_failed_and_restore_balance(self, request, queryset):
        restored_count = 0
        marked_count = 0
        for payout in queryset.select_related('wallet'):
            if payout.status != PayoutLog.PayoutStatus.FAILED:
                payout.status = PayoutLog.PayoutStatus.FAILED
                marked_count += 1
            if not payout.balance_restored and payout.amount > 0:
                wallet = payout.wallet
                wallet.available_balance += payout.amount
                wallet.save(update_fields=['available_balance', 'updated_at'])
                payout.balance_restored = True
                restored_count += 1
            payout.save(update_fields=['status', 'balance_restored', 'updated_at'])
        self.message_user(
            request,
            f"Updated {marked_count} payout(s) to Failed and restored balance on {restored_count} payout(s).",
            level=messages.WARNING,
        )