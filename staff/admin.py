# admin.py
from django.contrib import admin, messages
from .models import StaffProfile, AgentProfile


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user_email',
        'department',
        'is_available_for_assignment',
        'assigned_merchant_count',
        'max_merchant_capacity',
        'updated_at',
    )
    list_filter = ('department', 'is_available_for_assignment', 'created_at')
    list_editable = ('department', 'is_available_for_assignment', 'max_merchant_capacity')
    search_fields = ('user__email', 'user__first_name', 'user__last_name', 'department')
    readonly_fields = ('created_at', 'updated_at', 'assigned_merchant_count')
    autocomplete_fields = ('user',)
    ordering = ('user__email',)
    actions = ['mark_available_for_assignment', 'mark_unavailable_for_assignment']

    fieldsets = (
        ('Staff Account & Department', {
            'fields': ('user', 'department')
        }),
        ('Merchant Assignment Capacity', {
            'fields': ('is_available_for_assignment', 'max_merchant_capacity', 'assigned_merchant_count')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @admin.display(description='Staff Email', ordering='user__email')
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description='Assigned Merchants')
    def assigned_merchant_count(self, obj):
        return obj.user.managed_vendors.count()

    @admin.action(description="Mark selected staff as available for merchant assignment")
    def mark_available_for_assignment(self, request, queryset):
        updated = queryset.update(is_available_for_assignment=True)
        self.message_user(request, f"{updated} staff profile(s) marked available.", level=messages.SUCCESS)

    @admin.action(description="Mark selected staff as unavailable for merchant assignment")
    def mark_unavailable_for_assignment(self, request, queryset):
        updated = queryset.update(is_available_for_assignment=False)
        self.message_user(request, f"{updated} staff profile(s) marked unavailable.", level=messages.WARNING)


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user_email',
        'is_online',
        'active_chat_count',
        'max_chat_capacity',
        'updated_at',
    )
    list_filter = ('is_online', 'created_at')
    list_editable = ('is_online', 'max_chat_capacity')
    search_fields = ('user__email', 'user__first_name', 'user__last_name')
    readonly_fields = ('created_at', 'updated_at', 'active_chat_count')
    autocomplete_fields = ('user',)
    ordering = ('user__email',)
    actions = ['set_agents_online', 'set_agents_offline']

    fieldsets = (
        ('Support Agent Account', {
            'fields': ('user',)
        }),
        ('Live Chat Status & Capacity', {
            'fields': ('is_online', 'max_chat_capacity', 'active_chat_count')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @admin.display(description='Agent Email', ordering='user__email')
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description='Active Chats')
    def active_chat_count(self, obj):
        return obj.user.agent_assigned_chats.filter(is_active=True).count()

    @admin.action(description="Set selected agents as Online")
    def set_agents_online(self, request, queryset):
        updated = queryset.update(is_online=True)
        self.message_user(request, f"{updated} agent profile(s) set to Online.", level=messages.SUCCESS)

    @admin.action(description="Set selected agents as Offline")
    def set_agents_offline(self, request, queryset):
        updated = queryset.update(is_online=False)
        self.message_user(request, f"{updated} agent profile(s) set to Offline.", level=messages.WARNING)