import logging

from ninja import Router
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from ninja.errors import HttpError
from typing import List
from decimal import Decimal
from accounts.utils import JWTAuth
from audit.decorators import log_system_action
from .models import SystemBroadcast, PromoCode
from .schemas import BroadcastCreateSchema, PromoCodeCreateSchema

logger = logging.getLogger(__name__)

router = Router(tags=["Marketing Management"], auth=JWTAuth())

@router.post("/admin/broadcasts/send")
@log_system_action(
    action_name="SEND_BROADCAST",
    target_getter=lambda req, payload: f"Broadcast Audience: {payload.target_audience}"
)
def send_system_broadcast(request, payload: BroadcastCreateSchema):
    user = request.auth

    # Platform-wide messages may only be sent by management. Without this check any
    # logged-in customer/rider/merchant could push a phishing message to every user.
    if not (user.is_superuser or getattr(user, 'role', None) in ('admin', 'staff')):
        raise HttpError(403, "Access restricted.")

    # Map target audience to appropriate WebSocket channel group
    group_name_map = {
        'all': 'platform_global_broadcasts',
        'vendors': 'vendor_broadcasts',
        'merchants': 'vendor_broadcasts',
        'riders': 'rider_broadcasts',
        'customers': 'customer_broadcasts'
    }
    if payload.target_audience not in group_name_map:
        # An unknown audience used to fall through to a broadcast to EVERYONE.
        raise HttpError(400, "Invalid target audience.")
    if not payload.title.strip() or not payload.message.strip():
        raise HttpError(400, "Title and message are required.")

    broadcast = SystemBroadcast.objects.create(
        title=payload.title,
        message=payload.message,
        target_audience=payload.target_audience,
        sent_by=user
    )

    # BROADCAST LIVE VIA WEBSOCKETS TO TARGETED AUDIENCE
    channel_layer = get_channel_layer()
    target_group = group_name_map[payload.target_audience]

    if channel_layer:
        try:
            async_to_sync(channel_layer.group_send)(
                target_group,
                {
                    "type": "system_broadcast_message",
                    "message": {
                        "title": broadcast.title,
                        "text": broadcast.message,
                        "audience": broadcast.target_audience,
                        "created_at": broadcast.created_at.isoformat()
                    }
                }
            )
        except Exception:
            logger.exception("Broadcast WebSocket push failed for broadcast %s", broadcast.id)

    return {"status": "success", "message": "Broadcast transmitted and pushed via WebSocket successfully."}

@router.get("/admin/promos", response=List[dict])
def list_promo_codes(request):
    if request.user.role != 'admin':
        raise HttpError(403, "Access restricted.")
    
    promos = PromoCode.objects.all().order_by('-created_at')
    return [
        {
            "id": p.id,
            "code": p.code,
            "discount_type": p.discount_type,
            "value": float(p.value),
            "min_order_amount": float(p.min_order_amount),
            "is_active": p.is_active,
            "usage_limit": p.usage_limit,
            "times_used": p.times_used,
            "expires_at": p.expires_at,
        } for p in promos
    ]

@router.post("/admin/promos/create")
@log_system_action(
    action_name="CREATE_PROMO_CODE",
    target_getter=lambda req, payload: f"Promo Code: {payload.code}"
)
def create_promo_code(request, payload: PromoCodeCreateSchema):
    if request.user.role != 'admin':
        raise HttpError(403, "Access restricted.")
    
    promo = PromoCode.objects.create(
        code=payload.code.upper(),
        discount_type=payload.discount_type,
        value=Decimal(str(payload.value)),
        min_order_amount=Decimal(str(payload.min_order_amount)),
        usage_limit=payload.usage_limit,
        expires_at=payload.expires_at
    )
    return {"status": "success", "message": f"Promo code '{promo.code}' created successfully."}