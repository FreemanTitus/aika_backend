from django.db import models
from django.conf import settings

class Conversation(models.Model):
    class ConversationType(models.TextChoices):
        CUSTOMER_SUPPORT = 'customer_support', 'Customer & Agent'
        VENDOR_SUPPORT = 'vendor_support', 'Vendor & Allocated Staff'
        ADMIN_BROADCAST = 'admin_broadcast', 'Admin Direct'

    id = models.CharField(max_length=50, primary_key=True) # e.g., CONV-XXXX
    conversation_type = models.CharField(max_length=30, choices=ConversationType.choices)
    
    # Initiator (Customer or Vendor)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='customer_chats'
    )

    # Fallback: Populated if the user is a guest visitor
    guest_name = models.CharField(max_length=150, blank=True, null=True)
    guest_email = models.EmailField(blank=True, null=True)
    # SHA-256 of the secret handed to a guest when the chat starts. Without it nobody can
    # read or join a guest conversation, even if they somehow learn the conversation id.
    guest_token_hash = models.CharField(max_length=64, blank=True, default='')
    
    # Assigned Respondent (Agent for customers, allocated Staff for vendors, or Admin)
    assigned_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='agent_assigned_chats'
    )
    
    # If it's a vendor conversation, link the specific vendor profile
    vendor_profile = models.ForeignKey('merchant.MerchantProfile', on_delete=models.SET_NULL, null=True, blank=True)
    
    subject = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def display_name(self):
        if self.customer:
            return getattr(self.customer, 'email', 'Authenticated User')
        return self.guest_name or 'Anonymous Guest'

    @property
    def display_email(self):
        if self.customer:
            return getattr(self.customer, 'email', '')
        return self.guest_email or ''

    def __str__(self):
        return f"[{self.conversation_type}] {self.id} - Initiated by {self.display_name}"

class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True
    )
    content = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        sender = self.sender.email if self.sender_id else "Guest/System"
        return f"Msg from {sender} in {self.conversation_id}"