import json
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from accounts.utils import verify_jwt_token
from merchant.models import MerchantProfile

logger = logging.getLogger(__name__)


class VendorNotificationConsumer(AsyncWebsocketConsumer):
    """Vendor dashboard feed. Only the vendor's owner (or management) may join `vendor_<id>`."""

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

            target_vendor_id = (self.scope['url_route']['kwargs'].get('vendor_id') or '').strip()

            @database_sync_to_async
            def verify_vendor_access(user, vendor_id):
                if user.is_superuser or user.is_staff or getattr(user, 'role', None) in ['admin', 'staff']:
                    return True
                return MerchantProfile.objects.filter(id=vendor_id, user=user).exists()

            if not await verify_vendor_access(self.user, target_vendor_id):
                await self.close(code=4003)
                return

            self.vendor_id = target_vendor_id
            self.group_names = [
                f"vendor_{self.vendor_id}",
                "vendor_broadcasts",
                "platform_global_broadcasts",
            ]
            for group in self.group_names:
                await self.channel_layer.group_add(group, self.channel_name)

            await self.accept()

        except Exception:
            logger.exception("VendorNotificationConsumer connection error")
            await self.close(code=4000)

    async def disconnect(self, close_code):
        for group in getattr(self, 'group_names', []):
            await self.channel_layer.group_discard(group, self.channel_name)

    # ----- event handlers (payloads are read defensively: a missing key never raises) -----
    async def send_order_notification(self, event):
        await self.send(text_data=json.dumps({
            "event": "new_order",
            "order_id": str(event.get("order_id", "")),
            "total_price": event.get("total_price", 0),
            "delivery_address": event.get("delivery_address", ""),
            "items_summary": event.get("items_summary") or [],
            "customer_name": event.get("customer_name", ""),
            "customer_phone": event.get("customer_phone", ""),
        }))

    async def broadcast_dashboard_event(self, event):
        await self.send(text_data=json.dumps(event.get('payload') or {}))

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
