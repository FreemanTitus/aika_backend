import json
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from accounts.utils import verify_jwt_token

logger = logging.getLogger(__name__)


class RiderDashboardConsumer(AsyncWebsocketConsumer):
    @database_sync_to_async
    def get_user_from_jwt(self, token_key):
        return verify_jwt_token(token_key)

    async def connect(self):
        try:
            query_params = parse_qs(self.scope.get('query_string', b'').decode(errors='ignore'))
            token_key = query_params.get('token', [None])[0]

            user = None
            if token_key:
                user = await self.get_user_from_jwt(token_key)

            if not user or user.is_anonymous or not user.is_active:
                await self.close(code=4003)
                return

            # Only logistics riders or administrators may connect to the rider desk.
            user_role = (getattr(user, 'role', '') or '').lower()
            if user_role != 'rider' and not user.is_superuser:
                await self.close(code=4003)
                return

            self.joined_groups = [
                f"rider_{user.id}",          # personal group
                "rider_job_pool",            # newly acknowledged orders
                "rider_broadcasts",          # marketing / system broadcasts
                "platform_global_broadcasts",
            ]
            for group in self.joined_groups:
                await self.channel_layer.group_add(group, self.channel_name)

            await self.accept()

        except Exception:
            logger.exception("RiderDashboardConsumer connection error")
            await self.close(code=4000)

    async def disconnect(self, close_code):
        for group in getattr(self, 'joined_groups', []):
            await self.channel_layer.group_discard(group, self.channel_name)

    # ----- event handlers (payloads are read defensively) -----
    async def update_rider_status(self, event):
        await self.send(text_data=json.dumps(event.get('payload') or {}))

    async def wallet_update_alert(self, event):
        await self.send(text_data=json.dumps(event.get("payload") or {}))

    async def broadcast_dashboard_event(self, event):
        await self.send(text_data=json.dumps(event.get("payload") or {}))

    async def send_order_alert(self, event):
        """Broadcast from `rider_job_pool` when a merchant acknowledges an order."""
        await self.send(text_data=json.dumps(event.get("payload") or {}))

    async def order_completed_alert(self, event):
        payload = event.get("payload") or {}
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
