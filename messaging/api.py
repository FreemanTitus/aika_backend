import hashlib
import hmac
import logging
import secrets

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from ninja import Router
from ninja.errors import HttpError

from accounts.utils import JWTAuth, verify_jwt_token
from .models import Conversation, Message
from .schemas import StartChatSchema

logger = logging.getLogger(__name__)

router = Router(tags=["Messaging Hub"])

MAX_CHAT_MESSAGE_LENGTH = 2000


def hash_guest_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def guest_token_matches(conv, raw_token) -> bool:
    """Constant-time check of a guest's secret against the stored hash."""
    if not raw_token or not conv.guest_token_hash:
        return False
    return hmac.compare_digest(hash_guest_token(raw_token), conv.guest_token_hash)


def _optional_user(request):
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        try:
            user = verify_jwt_token(header.split(" ", 1)[1].strip())
        except Exception:
            return None
        return user if (user and user.is_active) else None
    return None


@router.post("/chat/start", auth=None)
def start_support_chat(request, payload: StartChatSchema):
    user = _optional_user(request)

    initial_message = (payload.initial_message or "").strip()
    if not initial_message:
        raise HttpError(400, "Please type a message to start the chat.")
    if len(initial_message) > MAX_CHAT_MESSAGE_LENGTH:
        raise HttpError(400, f"Messages are limited to {MAX_CHAT_MESSAGE_LENGTH} characters.")

    guest_name = None
    guest_email = None
    guest_token = None
    if not user:
        guest_name = (payload.full_name or "").strip()
        guest_email = (payload.email or "").strip()
        if not guest_name or not guest_email:
            raise HttpError(400, "Guest users must provide a full name and email address.")
        if len(guest_name) > 150:
            raise HttpError(400, "Name is too long.")
        try:
            validate_email(guest_email)
        except ValidationError:
            raise HttpError(400, "Please provide a valid email address.")
        guest_token = secrets.token_urlsafe(24)

    # The id is an unguessable capability (128 bits), not a sequential/short code.
    conv_id = f"CONV-{secrets.token_urlsafe(16)}"

    with transaction.atomic():
        if user:
            conv = Conversation.objects.create(
                id=conv_id,
                conversation_type='customer_support',
                customer=user,
            )
        else:
            conv = Conversation.objects.create(
                id=conv_id,
                conversation_type='customer_support',
                guest_name=guest_name,
                guest_email=guest_email,
                guest_token_hash=hash_guest_token(guest_token),
            )

        Message.objects.create(
            conversation=conv,
            sender=user,  # None for guests
            content=initial_message,
        )

    # Alert agents only after the commit, and never let a push failure break the chat start.
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                "support_queue",
                {
                    "type": "queue_update_broadcast",
                    "action": "new_chat",
                    "conversation_id": conv.id,
                    "display_name": conv.display_name,
                },
            )
    except Exception:
        logger.exception("Support queue broadcast failed for conversation %s", conv.id)

    response = {
        "status": "success",
        "conversation_id": conv.id,
        "message": "Support chat initialized successfully.",
    }
    if guest_token:
        # Shown once. The guest must send it as ?guest_token=... on the WebSocket and as the
        # X-Chat-Token header when reading history.
        response["guest_token"] = guest_token
    return response


@router.get("/chat/active", auth=JWTAuth())
def get_active_support_chat(request):
    """Checks if the authenticated customer currently has an active, ongoing support chat."""
    user = request.auth
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication required.")

    active_conv = Conversation.objects.filter(
        conversation_type='customer_support',
        customer=user,
        is_active=True
    ).order_by('-created_at').first()

    return {
        "status": "success",
        "conversation_id": active_conv.id if active_conv else None
    }


@router.get("/chat/{conv_id}/messages")
def get_public_chat_messages(request, conv_id: str):
    """
    Message history of a support conversation. Access is limited to:
      * the logged-in customer who owns it,
      * the assigned agent (or any admin/staff),
      * a guest presenting the secret returned by /chat/start (X-Chat-Token header).
    Anything else gets the same 404 as a conversation that does not exist, so ids cannot be probed.
    """
    conv = Conversation.objects.select_related('customer', 'assigned_staff').filter(
        id=conv_id, conversation_type='customer_support'
    ).first()
    if conv is None:
        raise HttpError(404, "Conversation not found.")

    user = _optional_user(request)
    allowed = False
    if user:
        role = (getattr(user, 'role', '') or '').lower()
        allowed = (
            user.is_superuser
            or role in ('admin', 'staff')
            or (conv.customer_id is not None and conv.customer_id == user.id)
            or (role == 'agent' and conv.assigned_staff_id == user.id)
        )
    elif conv.customer_id is None:
        allowed = guest_token_matches(conv, request.headers.get("X-Chat-Token", ""))

    if not allowed:
        raise HttpError(404, "Conversation not found.")

    messages = Message.objects.filter(conversation=conv).select_related('sender').order_by('created_at')

    return {
        "status": "success",
        "conversation_id": conv.id,
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender.id if m.sender else None,
                "sender_email": m.sender.email if m.sender else conv.display_email,
                "sender_name": m.sender.get_full_name() if m.sender else conv.display_name,
                "content": m.content,
                "created_at": m.created_at.isoformat()
            } for m in messages
        ]
    }
