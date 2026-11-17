import json
import logging
from urllib.parse import parse_qs
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Conversation, Message
from django.contrib.auth import get_user_model
from accounts.utils import verify_jwt_token


User = get_user_model()

logger = logging.getLogger(__name__)


class SupportChatConsumer(AsyncJsonWebsocketConsumer):

    async def connect(self):
        try:
            raw_conv_id = self.scope['url_route']['kwargs'].get('conv_id', '')
            self.conv_id = raw_conv_id.strip('/') if raw_conv_id else 'queue'
            
            query_string = self.scope.get('query_string', b'').decode()
            query_params = parse_qs(query_string)
            token_key = query_params.get('token', [None])[0]

            self.user = None
            if token_key:
                self.user = await database_sync_to_async(verify_jwt_token)(token_key)

            if self.conv_id == 'queue':
                if not self.user or self.user.is_anonymous or not self.user.is_active:
                    await self.close(code=4003)
                    return
                
                user_role = getattr(self.user, 'role', '').lower()
                if user_role not in ['agent', 'admin']:
                    await self.close(code=4003)
                    return
                    
                self.room_group_name = 'support_queue'
            else:
                self.room_group_name = f"chat_{self.conv_id}"
                
                has_access = await database_sync_to_async(self.verify_chat_access)(
                    self.conv_id, self.user, query_params
                )
                if not has_access:
                    await self.close(code=4003)
                    return

            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            await self.accept()

        except Exception:
            logger.exception("SupportChatConsumer connection error")
            await self.close(code=4000)

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive_json(self, content):
        msg_type = content.get('type')

        if msg_type == 'chat_message':
            message_text = content.get('message', '').strip()
            if not message_text:
                return

            try:
                saved_msg = await database_sync_to_async(self.create_message)(
                    self.conv_id, self.user, message_text
                )
            except PermissionError:
                await self.close(code=4003)
                return

            sender_id, sender_email = await database_sync_to_async(self.get_sender_details)(self.user, saved_msg)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message_broadcast',
                    'message_id': saved_msg.id,
                    'sender_id': sender_id,
                    'sender_email': sender_email,
                    'message': saved_msg.content,
                    'created_at': saved_msg.created_at.isoformat()
                }
            )
        elif msg_type == 'typing':
            sender_role = 'agent' if (self.user and getattr(self.user, 'role', '').lower() in ['agent', 'admin']) else 'customer'
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'typing_indicator',
                    'sender_role': sender_role,
                    'is_typing': content.get('is_typing', False)
                }
            )

    async def typing_indicator(self, event):
        await self.send_json({
            'type': 'typing_indicator',
            'sender_role': event['sender_role'],
            'is_typing': event['is_typing']
        })

    async def chat_message_broadcast(self, event):
        await self.send_json({
            'type': 'chat_message',
            'message_id': event['message_id'],
            'sender_id': event['sender_id'],
            'sender_email': event['sender_email'],
            'message': event['message'],
            'created_at': event['created_at']
        })

    async def queue_update_broadcast(self, event):
        await self.send_json({
            'type': 'queue_update',
            'action': event['action'],
            'conversation_id': event['conversation_id']
        })

    async def chat_ended(self, event):
        await self.send_json({
            'type': 'chat_ended',
            'message': event['message']
        })
        await self.close()

    async def chat_message(self, event):
        await self.send_json({
            'type': 'chat_message',
            'message_id': event.get('id'),
            'sender_id': event.get('sender_id'),
            'sender_email': event.get('sender_email', ''),
            'message': event.get('content'),
            'created_at': event.get('created_at')
        })
        
    def verify_chat_access(self, conv_id, user, query_params):
        from .api import guest_token_matches  # local import: api imports models, avoid cycles at load

        try:
            conv = Conversation.objects.select_related('customer', 'assigned_staff').get(id=conv_id)

            # Authenticated user validation
            if user and user.is_authenticated and not user.is_anonymous:
                role = (getattr(user, 'role', '') or '').lower()
                if user.is_superuser or role in ['admin', 'staff']:
                    return True
                if role == 'agent' and (conv.assigned_staff_id == user.id or conv.assigned_staff_id is None):
                    return True
                return conv.customer_id is not None and conv.customer_id == user.id

            # Guests must present the secret issued by /chat/start (a plain conversation id is not enough).
            if conv.customer_id is None:
                guest_token = query_params.get('guest_token', [None])[0] if query_params else None
                return guest_token_matches(conv, guest_token)

            return False
        except Conversation.DoesNotExist:
            return False

    def create_message(self, conv_id, user, text):
        conv = Conversation.objects.get(id=conv_id)

        if not conv.is_active:
            raise PermissionError("This conversation has ended.")

        valid_sender = user if (user and not user.is_anonymous) else None
        return Message.objects.create(
            conversation=conv,
            sender=valid_sender,
            content=text[:2000]
        )

    def get_sender_details(self, user, saved_msg):
        if user and not user.is_anonymous:
            return user.id, user.email
        conv = saved_msg.conversation
        return None, conv.display_email