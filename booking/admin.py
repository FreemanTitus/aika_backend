# admin.py
from django.contrib import admin, messages
from .models import AbstractBooking, RideBooking, SendPackage


class BaseBookingAdmin(admin.ModelAdmin):
    readonly_fields = ('booking_reference', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    autocomplete_fields = ('user', 'assigned_rider')
    actions = ['mark_in_transit', 'mark_completed', 'mark_cancelled']

    def save_model(self, request, obj, form, change):
        # Automatically promote status to ASSIGNED when a rider is newly attached to a PENDING booking
        if obj.assigned_rider and obj.status == AbstractBooking.BookingStatus.PENDING:
            obj.status = AbstractBooking.BookingStatus.ASSIGNED
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark selected bookings as In Transit")
    def mark_in_transit(self, request, queryset):
        updated = queryset.exclude(status=AbstractBooking.BookingStatus.COMPLETED).update(
            status=AbstractBooking.BookingStatus.IN_TRANSIT
        )
        self.message_user(request, f"{updated} booking(s) marked as In Transit.", level=messages.INFO)

    @admin.action(description="Mark selected bookings as Completed")
    def mark_completed(self, request, queryset):
        updated = queryset.exclude(status=AbstractBooking.BookingStatus.COMPLETED).update(
            status=AbstractBooking.BookingStatus.COMPLETED
        )
        self.message_user(request, f"{updated} booking(s) marked as Completed.", level=messages.SUCCESS)

    @admin.action(description="Cancel selected bookings")
    def mark_cancelled(self, request, queryset):
        updated = queryset.exclude(status=AbstractBooking.BookingStatus.COMPLETED).update(
            status=AbstractBooking.BookingStatus.CANCELLED
        )
        self.message_user(request, f"{updated} booking(s) cancelled.", level=messages.WARNING)


@admin.register(RideBooking)
class RideBookingAdmin(BaseBookingAdmin):
    list_display = (
        'booking_reference',
        'customer_name',
        'customer_phone',
        'pickup_preview',
        'dropoff_preview',
        'fare_amount',
        'assigned_rider',
        'status',
        'created_at',
    )
    list_filter = ('status', 'created_at')
    search_fields = (
        'booking_reference',
        'customer_name',
        'customer_phone',
        'user__email',
        'pickup_address',
        'dropoff_address',
    )

    fieldsets = (
        ('Trip Reference & Status', {
            'fields': ('booking_reference', 'status', 'assigned_rider', 'user')
        }),
        ('Passenger Details', {
            'fields': ('customer_name', 'customer_phone')
        }),
        ('Route & Fare', {
            'fields': ('pickup_address', 'dropoff_address', 'fare_amount')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @admin.display(description='Pickup')
    def pickup_preview(self, obj):
        return (obj.pickup_address[:35] + '...') if len(obj.pickup_address) > 35 else obj.pickup_address

    @admin.display(description='Dropoff')
    def dropoff_preview(self, obj):
        return (obj.dropoff_address[:35] + '...') if len(obj.dropoff_address) > 35 else obj.dropoff_address


@admin.register(SendPackage)
class SendPackageAdmin(BaseBookingAdmin):
    list_display = (
        'booking_reference',
        'customer_name',
        'receiver_name',
        'receiver_phone',
        'fare_amount',
        'is_cash_on_delivery',
        'cod_amount',
        'assigned_rider',
        'status',
        'created_at',
    )
    list_filter = ('status', 'is_cash_on_delivery', 'created_at')
    search_fields = (
        'booking_reference',
        'customer_name',
        'customer_phone',
        'receiver_name',
        'receiver_phone',
        'package_description',
        'user__email',
        'pickup_address',
        'dropoff_address',
    )

    fieldsets = (
        ('Delivery Reference & Status', {
            'fields': ('booking_reference', 'status', 'assigned_rider', 'user')
        }),
        ('Sender & Receiver Contact', {
            'fields': (
                ('customer_name', 'customer_phone'),
                ('receiver_name', 'receiver_phone'),
            )
        }),
        ('Package & Route Details', {
            'fields': ('package_description', 'pickup_address', 'dropoff_address')
        }),
        ('Billing & Cash on Delivery', {
            'fields': ('fare_amount', 'is_cash_on_delivery', 'cod_amount')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )