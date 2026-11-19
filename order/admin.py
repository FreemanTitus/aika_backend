# admin.py
from django.contrib import admin, messages
from .models import Order, UserCartItem


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'order_reference',
        'sender_name',
        'receiver_name',
        'package_type',
        'delivery_fee',
        'rider_earning',
        'platform_fee',
        'is_cash_on_delivery',
        'cod_amount_to_collect',
        'assigned_rider',
        'status',
        'created_at',
    )
    list_filter = (
        'status',
        'package_type',
        'is_cash_on_delivery',
        'created_at',
    )
    list_editable = ('status',)
    search_fields = (
        'order_reference',
        'sender_name',
        'sender_phone',
        'receiver_name',
        'receiver_phone',
        'pickup_address',
        'delivery_address',
        'package_description',
        'sender__company_name',
        'receiver__first_name',
        'receiver__last_name',
        'receiver__user__email',
        'marketplace_order__id',
        'assigned_rider__user__email',
    )
    readonly_fields = (
        'order_reference',
        'rider_earning',
        'platform_fee',
        'created_at',
        'updated_at',
    )
    autocomplete_fields = ('sender', 'receiver', 'marketplace_order', 'assigned_rider')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    actions = [
        'mark_acknowledged',
        'mark_in_transit',
        'mark_delivered',
        'mark_completed',
        'mark_cancelled',
        'recalculate_order_splits',
    ]

    fieldsets = (
        ('Order Reference & Dispatch Status', {
            'fields': ('order_reference', 'status', 'assigned_rider', 'marketplace_order')
        }),
        ('Sender & Pickup Details', {
            'fields': ('sender', ('sender_name', 'sender_phone'), 'pickup_address')
        }),
        ('Receiver & Dropoff Details', {
            'fields': ('receiver', ('receiver_name', 'receiver_phone'), 'delivery_address')
        }),
        ('Package Information', {
            'fields': ('package_type', 'package_description')
        }),
        ('Financials & Splits', {
            'fields': (
                ('delivery_fee', 'rider_earning', 'platform_fee'),
                ('is_cash_on_delivery', 'cod_amount_to_collect'),
            )
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    def save_model(self, request, obj, form, change):
        # Promote pending/acknowledged orders to ASSIGNED when a rider is newly attached
        if obj.assigned_rider and obj.status in (
            Order.OrderStatus.PENDING,
            Order.OrderStatus.ACKNOWLEDGED,
        ):
            obj.status = Order.OrderStatus.ASSIGNED
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark selected orders as Acknowledged by Vendor")
    def mark_acknowledged(self, request, queryset):
        updated = queryset.update(status=Order.OrderStatus.ACKNOWLEDGED)
        self.message_user(request, f"{updated} order(s) marked as Acknowledged.", level=messages.INFO)

    @admin.action(description="Mark selected orders as In Transit")
    def mark_in_transit(self, request, queryset):
        updated = queryset.update(status=Order.OrderStatus.IN_TRANSIT)
        self.message_user(request, f"{updated} order(s) marked as In Transit.", level=messages.INFO)

    @admin.action(description="Mark selected orders as Delivered")
    def mark_delivered(self, request, queryset):
        updated = queryset.update(status=Order.OrderStatus.DELIVERED)
        self.message_user(request, f"{updated} order(s) marked as Delivered.", level=messages.SUCCESS)

    @admin.action(description="Mark selected orders as Completed")
    def mark_completed(self, request, queryset):
        updated = queryset.update(status=Order.OrderStatus.COMPLETED)
        self.message_user(request, f"{updated} order(s) marked as Completed.", level=messages.SUCCESS)

    @admin.action(description="Cancel selected orders")
    def mark_cancelled(self, request, queryset):
        updated = queryset.update(status=Order.OrderStatus.CANCELLED)
        self.message_user(request, f"{updated} order(s) cancelled.", level=messages.WARNING)

    @admin.action(description="Recalculate rider & platform splits for selected orders")
    def recalculate_order_splits(self, request, queryset):
        recalculated = 0
        for order in queryset.select_related('assigned_rider'):
            if order.assigned_rider:
                order.calculate_and_set_splits()
                order.save(update_fields=['rider_earning', 'platform_fee', 'updated_at'])
                recalculated += 1
        self.message_user(
            request,
            f"Recalculated splits for {recalculated} order(s) with an assigned rider.",
            level=messages.SUCCESS,
        )


@admin.register(UserCartItem)
class UserCartItemAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'name',
        'product_id',
        'vendor_name',
        'store_id',
        'quantity',
        'price',
        'line_total',
        'updated_at',
    )
    list_filter = ('updated_at',)
    list_editable = ('quantity',)
    search_fields = (
        'user__email',
        'user__phone_number',
        'product_id',
        'name',
        'vendor_name',
        'store_id',
    )
    readonly_fields = ('updated_at', 'line_total')
    autocomplete_fields = ('user',)
    date_hierarchy = 'updated_at'
    ordering = ('-updated_at',)

    @admin.display(description='Subtotal')
    def line_total(self, obj):
        return f"₦{(obj.price * obj.quantity):,.2f}"