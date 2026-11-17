from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

def dispatch_order_to_rider(rider_profile, order_details):
    # 1. SMARTPHONE: Send to WebSocket (Real-time App)
    if rider_profile.device_type == 'smartphone':
        channel_layer = get_channel_layer()
        group_name = f"rider_{rider_profile.user_id}"  # the rider socket joins rider_<user id>
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "broadcast_dashboard_event",
                "payload": {"event": "new_dispatch", "order": order_details}
            }
        )
    
    # 2. FEATURE_PHONE: Send to SMS gateway
    else:
        # Trigger your SMS provider (e.g., Twilio or Termii)
        # send_sms(rider_profile.phone_number, f"New delivery: {order_details['id']} - Check your SMS portal.")
        pass