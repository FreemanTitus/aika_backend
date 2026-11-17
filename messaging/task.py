from django.utils import timezone
from datetime import timedelta
from .models import Conversation

def close_idle_chats():
    threshold = timezone.now() - timedelta(minutes=10)
    idle_convs = Conversation.objects.filter(is_active=True, conversation_type='customer_support', updated_at__lt=threshold)
    for conv in idle_convs:
        conv.is_active = False
        conv.save(update_fields=['is_active'])