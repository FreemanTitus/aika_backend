import json
from channels.generic.websocket import AsyncWebsocketConsumer


class AdminNotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        try:
            self.user = self.scope.get("user")
            
            # 1. Authentication Connection Guard Layer
            if not self.user or self.user.is_anonymous or not self.user.is_active:
                await self.close(code=4003)
                return

            user_role = getattr(self.user, 'role', '').lower()
            is_super = self.user.is_superuser

            # Initialize tracking flags for clean disconnection
            self.joined_groups = []

            # Scenario A: Root Admin or Superuser -> Access to everything
            if user_role == 'admin' or is_super:
                self.global_group = "admin_notifications"
                await self.channel_layer.group_add(self.global_group, self.channel_name)
                self.joined_groups.append(self.global_group)
                
                # Add to live tracking desk group
                await self.channel_layer.group_add("admin_tracking_desk", self.channel_name)
                self.joined_groups.append("admin_tracking_desk")

            # Scenario B: Operational Staff Agent -> Isolate to assigned desk + tracking
            elif user_role == 'staff':
                self.agent_group = f"agent_operations_{self.user.id}"
                await self.channel_layer.group_add(self.agent_group, self.channel_name)
                self.joined_groups.append(self.agent_group)
                
                # Staff agents also monitor live tracking
                await self.channel_layer.group_add("admin_tracking_desk", self.channel_name)
                self.joined_groups.append("admin_tracking_desk")
            
            # Scenario C: Non-management accounts are strictly blocked
            else:
                await self.close(code=4003)
                return

            await self.accept()

        except Exception as e:
            print(f"AdminNotificationConsumer connection error: {e}")
            await self.close(code=4000)

    async def disconnect(self, close_code):
        # Safely discard only the groups this specific connection joined
        if hasattr(self, 'joined_groups'):
            for group in self.joined_groups:
                await self.channel_layer.group_discard(group, self.channel_name)

    # =========================================================================
    # EVENT HANDLERS (Invoked from async_to_sync calls in views)
    # =========================================================================

    async def send_order_alert(self, event):
        """ Receives payload from group_send for newly acknowledged vendor dispatches """
        payload = event.get("payload", {})

        broadcast_data = {
            "event": "new_order_alert",
            "order_id": payload.get("order_id", "N/A"),
            "logistics_ref": payload.get("logistics_ref"),
            "restaurant_name": payload.get("restaurant_name", "Processing Store"),
            "pickup_address": payload.get("pickup_address", "Merchant Location"),
            "is_assigned_directly": payload.get("is_assigned_directly", False),
            "message": payload.get("message", "Awaiting rider allocation assignment.")
        }

        await self.send(text_data=json.dumps(broadcast_data))
    
    async def order_completed_alert(self, event):
        """ Receives payload from group_send when riders/customers finish delivery """
        payload = event.get("payload", {})
        
        broadcast_data = {
            "event": "order_completed",
            "order_reference": payload.get("order_reference"),
            "message": payload.get("message")
        }
        
        await self.send(text_data=json.dumps(broadcast_data))

    async def admin_notification_message(self, event):
        """ 
        Receives payload from group_send for customer disputes and urgent claims. 
        Flags event as 'new_dispute' to trigger the admin audio chime. 
        """
        message_payload = event.get("message", {})

        broadcast_data = {
            "event": "new_dispute",  # Triggers audio chime in useAdminSocket.ts
            "title": message_payload.get("title", "New Dispute Claim"),
            "text": message_payload.get("text"),
            "dispute_id": message_payload.get("dispute_id"),
            "created_at": message_payload.get("created_at")
        }

        await self.send(text_data=json.dumps(broadcast_data))

    async def rider_location_broadcast(self, event):
        """ Receives coordinate payload from riders and relays it to the admin tracking desk """
        payload = event.get("payload", {})
        await self.send(text_data=json.dumps(payload))

    async def broadcast_dashboard_event(self, event):
        """ Generic dashboard event (e.g. rider status changes) relayed to the admin desk """
        await self.send(text_data=json.dumps(event.get("payload") or {}))

    async def system_broadcast_message(self, event):
        message = event.get("message") or event.get("broadcast") or {}
        await self.send(text_data=json.dumps({
            "event": "system_broadcast_message",
            "title": message.get("title"),
            "text": message.get("text") or message.get("message"),
            "audience": message.get("audience"),
            "created_at": message.get("created_at"),
        }))
