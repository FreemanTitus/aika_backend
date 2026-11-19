# admin.py
from django.contrib import admin, messages
from .models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    can_delete = False
    fields = ('created_at', 'sender', 'content', 'is_read')
    readonly_fields = ('created_at', 'sender', 'content')
    ordering = ('created_at',)
    autocomplete_fields = ('sender',)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'conversation_type',
        'initiator_display',
        'subject',
        'assigned_staff',
        'vendor_profile',
        'is_active',
        'message_count',
        'updated_at',
    )
    list_filter = ('conversation_type', 'is_active', 'created_at', 'updated_at')
    list_editable = ('is_active', 'assigned_staff')
    search_fields = (
        'id',
        'subject',
        'customer__email',
        'guest_name',
        'guest_email',
        'assigned_staff__email',
        'vendor_profile__company_name',
    )
    readonly_fields = ('guest_token_hash', 'created_at', 'updated_at', 'initiator_display')
    autocomplete_fields = ('customer', 'assigned_staff', 'vendor_profile')
    inlines = [MessageInline]
    date_hierarchy = 'created_at'
    ordering = ('-updated_at',)
    actions = ['mark_active', 'close_conversations']

    fieldsets = (
        ('Conversation Metadata', {
            'fields': ('id', 'conversation_type', 'subject', 'is_active')
        }),
        ('Initiator & Guest Security', {
            'fields': ('customer', 'initiator_display', 'guest_name', 'guest_email', 'guest_token_hash')
        }),
        ('Assignment & Context', {
            'fields': ('assigned_staff', 'vendor_profile')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    @admin.display(description='Initiator')
    def initiator_display(self, obj):
        email = obj.display_email
        return f"{obj.display_name} ({email})" if email and email != obj.display_name else obj.display_name

    @admin.display(description='Messages')
    def message_count(self, obj):
        return obj.messages.count()

    @admin.action(description="Reopen selected conversations")
    def mark_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} conversation(s) marked as active.", level=messages.SUCCESS)

    @admin.action(description="Close selected conversations")
    def close_conversations(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} conversation(s) marked as closed.", level=messages.WARNING)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'sender_display', 'short_content', 'is_read', 'created_at')
    list_filter = ('is_read', 'conversation__conversation_type', 'created_at')
    list_editable = ('is_read',)
    search_fields = ('conversation__id', 'sender__email', 'content')
    readonly_fields = ('created_at',)
    autocomplete_fields = ('conversation', 'sender')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    actions = ['mark_messages_read', 'mark_messages_unread']

    @admin.display(description='Sender', ordering='sender__email')
    def sender_display(self, obj):
        return obj.sender.email if obj.sender_id else "Guest / System"

    @admin.display(description='Message')
    def short_content(self, obj):
        return (obj.content[:70] + '...') if len(obj.content) > 70 else obj.content

    @admin.action(description="Mark selected messages as read")
    def mark_messages_read(self, request, queryset):
        updated = queryset.update(is_read=True)
        self.message_user(request, f"{updated} message(s) marked as read.", level=messages.SUCCESS)

    @admin.action(description="Mark selected messages as unread")
    def mark_messages_unread(self, request, queryset):
        updated = queryset.update(is_read=False)
        self.message_user(request, f"{updated} message(s) marked as unread.", level=messages.INFO)