from django.db import models
from django.conf import settings

class SystemAuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255) # e.g., "SUSPEND_VENDOR"
    target_entity = models.CharField(max_length=100) # e.g., "Vendor: Tasty Bites"
    details = models.TextField(blank=True, null=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        username = self.user.email if self.user else "System"
        return f"[{self.created_at}] {username} - {self.action}"

class OrderDisputeRefund(models.Model):
    class StatusChoice(models.TextChoices):
        PENDING = 'pending', 'Pending Review'
        APPROVED = 'approved', 'Refund Approved'
        REJECTED = 'rejected', 'Refund Rejected'

    order_id = models.CharField(max_length=50)
    customer_name = models.CharField(max_length=150)
    reason = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=StatusChoice.choices, default=StatusChoice.PENDING)
    raised_by = models.CharField(max_length=100) # Customer or Agent name
    
    # New Manual Bank Payout Details
    bank_name = models.CharField(max_length=100, blank=True, null=True)
    account_number = models.CharField(max_length=20, blank=True, null=True)
    account_name = models.CharField(max_length=150, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"Dispute #{self.id} - Order #{self.order_id} ({self.status})"