# admin.py
from django.contrib import admin, messages
from .models import SystemAuditLog, OrderDisputeRefund


@admin.register(SystemAuditLog)
class SystemAuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor_display', 'action', 'target_entity', 'ip_address')
    list_filter = ('action', 'created_at')
    search_fields = ('user__email', 'action', 'target_entity', 'details', 'ip_address')
    readonly_fields = ('user', 'action', 'target_entity', 'details', 'ip_address', 'created_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    @admin.display(description='Actor', ordering='user__email')
    def actor_display(self, obj):
        return obj.user.email if obj.user else 'System'

    # Keep audit logs strictly immutable in Django Admin
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderDisputeRefund)
class OrderDisputeRefundAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'order_id',
        'customer_name',
        'amount',
        'status',
        'bank_name',
        'account_number',
        'raised_by',
        'resolved_by',
        'created_at',
    )
    list_filter = ('status', 'bank_name', 'created_at')
    search_fields = (
        'order_id',
        'customer_name',
        'raised_by',
        'account_number',
        'account_name',
        'reason',
    )
    readonly_fields = ('created_at', 'resolved_by')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    actions = ['approve_refunds', 'reject_refunds']

    fieldsets = (
        ('Dispute Overview', {
            'fields': ('order_id', 'customer_name', 'raised_by', 'amount', 'reason')
        }),
        ('Manual Bank Payout Details', {
            'fields': ('bank_name', 'account_number', 'account_name')
        }),
        ('Resolution & Tracking', {
            'fields': ('status', 'resolved_by', 'created_at')
        }),
    )

    def save_model(self, request, obj, form, change):
        # Automatically record who resolved the dispute when status moves out of PENDING
        if change and 'status' in form.changed_data:
            if obj.status in (OrderDisputeRefund.StatusChoice.APPROVED, OrderDisputeRefund.StatusChoice.REJECTED):
                obj.resolved_by = request.user
            elif obj.status == OrderDisputeRefund.StatusChoice.PENDING:
                obj.resolved_by = None
        super().save_model(request, obj, form, change)

    @admin.action(description="Approve selected refund disputes")
    def approve_refunds(self, request, queryset):
        updated = queryset.exclude(status=OrderDisputeRefund.StatusChoice.APPROVED).update(
            status=OrderDisputeRefund.StatusChoice.APPROVED,
            resolved_by=request.user,
        )
        self.message_user(request, f"{updated} refund dispute(s) marked as approved.", level=messages.SUCCESS)

    @admin.action(description="Reject selected refund disputes")
    def reject_refunds(self, request, queryset):
        updated = queryset.exclude(status=OrderDisputeRefund.StatusChoice.REJECTED).update(
            status=OrderDisputeRefund.StatusChoice.REJECTED,
            resolved_by=request.user,
        )
        self.message_user(request, f"{updated} refund dispute(s) marked as rejected.", level=messages.WARNING)