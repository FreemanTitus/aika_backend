import json
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer, AsyncWebsocketConsumer

from accounts.utils import verify_jwt_token

logger = logging.getLogger(__name__)


class CustomerTrackingConsumer(AsyncWebsocketConsumer):
    """
    Live order tracking for ONE customer. Authenticated by JWT in the query string and
    restricted to the owner (or management). Every group_send type that the backend
    emits to `customer_<user_id>` has a handler below: Channels raises
    "No handler for message type" for an unknown type, which kills the connection.
    """

    async def connect(self):
        try:
            query_params = parse_qs(self.scope.get('query_string', b'').decode(errors='ignore'))
            token_key = query_params.get('token', [None])[0]

            self.user = None
            if token_key:
                self.user = await database_sync_to_async(verify_jwt_token)(token_key)

            if not self.user or self.user.is_anonymous or not self.user.is_active:
                await self.close(code=4003)
                return

            target_user_id = self.scope['url_route']['kwargs'].get('user_id')

            is_owner = str(self.user.id) == str(target_user_id)
            is_management = (
                self.user.is_superuser or self.user.is_staff
                or getattr(self.user, 'role', None) in ['admin', 'staff', 'agent']
            )
            if not (is_owner or is_management):
                await self.close(code=4003)
                return

            self.group_names = [
                f"customer_{target_user_id}",
                "customer_broadcasts",
                "platform_global_broadcasts",
            ]
            for group in self.group_names:
                await self.channel_layer.group_add(group, self.channel_name)
            await self.accept()

        except Exception:
            logger.exception("CustomerTrackingConsumer connection error")
            await self.close(code=4000)

    async def disconnect(self, close_code):
        for group in getattr(self, 'group_names', []):
            await self.channel_layer.group_discard(group, self.channel_name)

    # ----- event handlers (payloads are read defensively: a missing key never raises) -----
    async def broadcast_dashboard_event(self, event):
        await self.send(text_data=json.dumps(event.get('payload') or {}))

    async def order_in_transit_alert(self, event):
        payload = event.get('payload') or {}
        await self.send(text_data=json.dumps({
            "event": "order_in_transit",
            "order_reference": payload.get("order_reference"),
            "message": payload.get("message"),
        }))

    async def order_completed_alert(self, event):
        payload = event.get('payload') or {}
        await self.send(text_data=json.dumps({
            "event": "order_completed",
            "order_reference": payload.get("order_reference"),
            "message": payload.get("message"),
        }))

    async def system_broadcast_message(self, event):
        message = event.get("message") or event.get("broadcast") or {}
        await self.send(text_data=json.dumps({
            "event": "system_broadcast_message",
            "title": message.get("title"),
            "text": message.get("text") or message.get("message"),
            "audience": message.get("audience"),
            "created_at": message.get("created_at"),
        }))


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        # The user is populated by TokenAuthMiddleware.
        self.user = self.scope.get("user")

        if not self.user or self.user.is_anonymous or not self.user.is_active:
            await self.close(code=4003)
            return

        self.group_name = f"user_notifications_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def send_notification(self, event):
        # NOTE: the Channels dispatch key is event["type"] ("send_notification"); the
        # notification's own kind therefore travels in "notification_type".
        await self.send_json({
            "id": event.get("id"),
            "type": event.get("notification_type", "notification"),
            "message": event.get("message"),
            "timestamp": event.get("timestamp"),
            "read": False,
            "raw_id": event.get("raw_id"),
        })
