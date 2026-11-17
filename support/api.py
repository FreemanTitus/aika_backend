from ninja import Router, Schema
from django.shortcuts import get_object_or_404
from django.db import transaction
from ninja.errors import HttpError
from accounts.utils import JWTAuth
from messaging.models import Conversation, Message
from channels.layers import get_channel_layer
from asgiref.sync import sync_to_async
from .schemas import ReplySchema
from messaging.task import close_idle_chats
from accounts.security import RequireRole


router = Router(tags=["Agent Support Queue"], auth=RequireRole(allowed_roles=['agent']))



# 1. Get Open Queue (Unassigned customer chats) & Agent's Active Chats
@router.get("/queues")
def get_agent_queues(request):
    user = request.auth
    if user.role != 'agent':
        raise HttpError(403, "Access restricted to Support Agents.")

    # Unassigned incoming customer chats waiting for an agent
    unassigned_chats = Conversation.objects.filter(
        conversation_type='customer_support',
        assigned_staff__isnull=True,
        is_active=True
    ).select_related('customer').order_by('-created_at')

    # Chats currently claimed and handled by this specific agent
    my_active_chats = Conversation.objects.filter(
        conversation_type='customer_support',
        assigned_staff=user,
        is_active=True
    ).select_related('customer').order_by('-updated_at')

    # close_idle_chats()

    return {
        "status": "success",
        "unassigned_queue": [
            {
                "id": c.id,
                "customer_name": c.display_name,
                "customer_email": c.display_email,
                "created_at": c.created_at.isoformat()
            } for c in unassigned_chats
        ],
        "my_active_chats": [
            {
                "id": c.id,
                "customer_name": c.display_name,
                "customer_email": c.display_email,
                "updated_at": c.updated_at.isoformat()
            } for c in my_active_chats
        ]
    }

@router.post("/chat/{conv_id}/join")
async def join_customer_chat(request, conv_id: str):
    user = request.auth

    @sync_to_async
    def execute_join():
        with transaction.atomic():
            try:
                conv = Conversation.objects.select_for_update().get(
                    id=conv_id, conversation_type='customer_support'
                )
            except Conversation.DoesNotExist:
                raise HttpError(404, "Conversation not found.")

            if not conv.is_active:
                raise HttpError(400, "This conversation has already ended.")

            if conv.assigned_staff_id and conv.assigned_staff_id != user.id:
                raise HttpError(400, "This conversation has already been claimed by another agent.")

            conv.assigned_staff = user
            conv.save(update_fields=['assigned_staff', 'updated_at'])

            msg_content = "You are now connected with a support agent."

            # Save without assigning a specific user sender so it's a neutral system message
            new_msg = Message.objects.create(
                conversation=conv,
                sender=None,
                content=msg_content
            )
            return conv.id, new_msg.id, new_msg.created_at, msg_content

    conv_id_val, msg_id, msg_created_at, msg_content = await execute_join()

    # Broadcast via WebSocket to the chat room using native await
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"chat_{conv_id_val}",
            {
                "type": "chat_message",
                "id": msg_id,
                "content": msg_content,
                "sender_id": None,
                "sender_role": "system",
                "created_at": msg_created_at.isoformat(),
            }
        )

        # Update queue for all agents
        await channel_layer.group_send(
            "support_queue",
            {
                "type": "queue_update_broadcast",
                "action": "chat_claimed",
                "conversation_id": conv_id_val
            }
        )

    return {"status": "success", "message": "Successfully joined conversation."}

@router.get("/chat/{conv_id}/messages")
def get_chat_messages(request, conv_id: str):
    user = request.auth

    conv = get_object_or_404(Conversation, id=conv_id, conversation_type='customer_support')

    # Ensure the agent is assigned or has access
    if conv.assigned_staff_id != user.id and not (user.is_superuser or user.role == 'admin'):
        raise HttpError(403, "You are not assigned to this conversation.")

    messages = Message.objects.filter(conversation=conv).select_related('sender').order_by('created_at')

    return {
        "status": "success",
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender.id if m.sender else None,
                "sender_email": m.sender.email if m.sender else conv.display_email,
                "message": m.content,
                "created_at": m.created_at.isoformat()
            } for m in messages
        ]
    }

# 3. Agent sends a reply to an active customer chat
@router.post("/chat/{conv_id}/reply")
def agent_send_message(request, conv_id: str, payload: ReplySchema):
    user = request.auth

    conv = get_object_or_404(Conversation, id=conv_id, conversation_type='customer_support', assigned_staff=user)

    if not conv.is_active:
        raise HttpError(400, "Cannot reply to a closed conversation.")

    content = payload.content.strip()
    if not content:
        raise HttpError(400, "Message cannot be empty.")

    message = Message.objects.create(
        conversation=conv,
        sender=user,
        content=content[:2000]
    )

    # Touch updated_at timestamp to resort active chats
    conv.save(update_fields=['updated_at'])

    return {
        "status": "success",
        "message": {
            "id": message.id,
            "sender_email": user.email,
            "content": message.content,
            "created_at": message.created_at.isoformat()
        }
    }

@router.post("/chat/{conv_id}/end")
async def end_customer_chat(request, conv_id: str):
    user = request.auth

    @sync_to_async
    def execute_end():
        with transaction.atomic():
            try:
                conv = Conversation.objects.select_for_update().get(
                    id=conv_id, conversation_type='customer_support'
                )
            except Conversation.DoesNotExist:
                raise HttpError(404, "Conversation not found.")

            # Only the agent handling the chat (or an admin) may end it. Any agent used to be
            # able to close any customer's conversation, including ones assigned to others.
            if conv.assigned_staff_id != user.id and not (user.is_superuser or user.role == 'admin'):
                raise HttpError(403, "You are not assigned to this conversation.")

            conv.is_active = False
            conv.save(update_fields=['is_active', 'updated_at'])
            return conv.id

    conv_id_val = await execute_end()

    channel_layer = get_channel_layer()
    if channel_layer:
        # Broadcast closure event over WebSocket using native await
        await channel_layer.group_send(
            f"chat_{conv_id_val}",
            {
                "type": "chat_ended",
                "message": "This chat session has been ended by the support agent."
            }
        )

        # Also broadcast to queue to remove it from active lists
        await channel_layer.group_send(
            "support_queue",
            {
                "type": "queue_update_broadcast",
                "action": "chat_closed",
                "conversation_id": conv_id_val
            }
        )

    return {"status": "success", "message": "Conversation ended successfully."}