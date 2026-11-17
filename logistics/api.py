import math
import logging
from django.conf import settings
from ninja import Router
from ninja.errors import HttpError
from django.db import transaction
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync, sync_to_async
from accounts.models import CustomUser, CoverageArea
from accounts.utils import JWTAuth
from rider.models import RiderLocation, RiderProfile
from .schemas import LocationUpdateSchema
from order.models import Order as LogisticOrder
from rider.schemas import RiderStatusSchema


logger = logging.getLogger(__name__)


router = Router(auth=JWTAuth())



def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    d_lat = math.radians(float(lat2) - float(lat1))
    d_lon = math.radians(float(lon2) - float(lon1))
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(float(lat1)))
        * math.cos(math.radians(float(lat2)))
        * math.sin(d_lon / 2) ** 2
    )
    a = max(0.0, min(1.0, a))
    return 2 * radius_km * math.asin(math.sqrt(a))


@router.post("/update-location")
def update_rider_location(request, payload: LocationUpdateSchema):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)

    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")

    if getattr(user, 'role', None) != 'rider' and not getattr(user, 'is_staff', False):
        raise HttpError(403, "Access restricted to delivery riders.")

    try:
        rider_profile = RiderProfile.objects.select_related('user__base_location').get(user=user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found for this user account.")

    base_location = getattr(rider_profile.user, 'base_location', None)
    max_coverage_km = float(getattr(base_location, 'coverage_radius_km', 0) or 50.0)
    within_coverage = True

    if base_location:
        hub_lat = getattr(base_location, 'latitude', None)
        hub_lng = getattr(base_location, 'longitude', None)
        if hub_lat is not None and hub_lng is not None and not getattr(settings, 'DEBUG', False):
            distance_km = _haversine_km(hub_lat, hub_lng, payload.latitude, payload.longitude)
            if distance_km > max_coverage_km:
                within_coverage = False
                logger.warning(
                    "Rider %s blocked outside %s hub (%.1f km > %.1f km)",
                    user.email,
                    base_location.name,
                    distance_km,
                    max_coverage_km,
                )
                raise HttpError(
                    403,
                    f"Current GPS location is outside your assigned {base_location.name} coverage region ({distance_km:.1f} km away)."
                )
    else:
        if hasattr(CoverageArea, 'latitude') and hasattr(CoverageArea, 'longitude'):
            nearest_hub = None
            nearest_dist = None
            for hub in CoverageArea.objects.exclude(latitude__isnull=True, longitude__isnull=True):
                dist = _haversine_km(hub.latitude, hub.longitude, payload.latitude, payload.longitude)
                hub_radius = float(getattr(hub, 'coverage_radius_km', 0) or 50.0)
                if dist <= hub_radius and (nearest_dist is None or dist < nearest_dist):
                    nearest_hub = hub
                    nearest_dist = dist

            if nearest_hub:
                base_location = nearest_hub
                CustomUser.objects.filter(pk=user.pk).update(base_location=nearest_hub)
            elif not getattr(settings, 'DEBUG', False):
                raise HttpError(403, "No active regional delivery hub covers your current GPS location.")

    loc, _ = RiderLocation.objects.update_or_create(
        rider=rider_profile,
        defaults={
            'latitude': payload.latitude,
            'longitude': payload.longitude,
        }
    )

    CustomUser.objects.filter(pk=user.pk).update(
        latitude=payload.latitude,
        longitude=payload.longitude,
    )

    if rider_profile.status == 'offline' and within_coverage:
        rider_profile.status = 'available'
        rider_profile.save(update_fields=['status'])

    hub_id = base_location.id if base_location else None
    hub_name = str(base_location.name) if base_location else None

    channel_layer = get_channel_layer()
    ws_message = {
        "type": "rider_location_broadcast",
        "payload": {
            "event": "rider_location_update",
            "rider_id": rider_profile.id,
            "rider_name": getattr(rider_profile, 'full_name', None) or user.email,
            "base_location_id": hub_id,
            "base_location": hub_name,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "updated_at": loc.updated_at.isoformat(),
        }
    }
    async_to_sync(channel_layer.group_send)("admin_tracking_desk", ws_message)
    if hub_id:
        async_to_sync(channel_layer.group_send)(f"admin_tracking_desk_{hub_id}", ws_message)

    return {
        "status": "success",
        "message": "Location updated successfully.",
        "rider_status": rider_profile.status,
        "base_location_id": hub_id,
        "base_location": hub_name,
    }

@router.post("/go-offline")
def rider_go_offline(request):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")
    
    if getattr(user, 'role', None) != 'rider' and not getattr(user, 'is_staff', False):
        raise HttpError(403, "Access restricted to delivery riders.")

    try:
        rider_profile = RiderProfile.objects.get(user=user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found for this user account.")

    with transaction.atomic():
        # NOTE: We no longer delete RiderLocation here so coordinates are suspended for reuse
        
        # Automatically mark rider status as offline
        rider_profile.status = 'offline'
        rider_profile.save(update_fields=['status'])

    # Broadcast offline status change via WebSockets
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "admin_tracking_desk",
        {
            "type": "rider_location_broadcast",
            "payload": {
                "event": "rider_went_offline",
                "rider_id": rider_profile.id,
                "rider_name": getattr(rider_profile, 'full_name', None) or user.email,
            }
        }
    )
    
    async_to_sync(channel_layer.group_send)(
        "admin_tracking_desk",
        {
            "type": "broadcast_dashboard_event",
            "payload": {
                "event": "rider_status_change",
                "rider_id": rider_profile.id,
                "new_status": "offline"
            }
        }
    )

    return {"status": "success", "message": "Rider successfully went offline."}

@router.post("/dashboard/status")
async def update_rider_status(request, data: RiderStatusSchema):
    if data.status not in ['available', 'busy', 'offline']:
        raise HttpError(400, "Invalid status")
        
    try:
        rider = await RiderProfile.objects.aget(user=request.user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    rider.status = data.status
    await sync_to_async(rider.save)()

    # Location is preserved in database even when status becomes offline
    
    channel_layer = get_channel_layer()
    await channel_layer.group_send(
        "admin_tracking_desk",
        {
            "type": "broadcast_dashboard_event",
            "payload": {
                "event": "rider_status_change",
                "rider_id": rider.id,
                "new_status": rider.status
            }
        }
    )
    
    return {"status": "success", "new_status": rider.status}

@router.get("/tracking-status")
def get_rider_tracking_status(request):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication credentials required.")
    
    try:
        rider_profile = RiderProfile.objects.get(user=user)
    except RiderProfile.DoesNotExist:
        return {"is_online": False, "status": "offline"}

    is_online = RiderLocation.objects.filter(rider=rider_profile).exists()
    
    return {
        "is_online": is_online,
        "status": rider_profile.status
    }

@router.get('/deliveries')
async def list_rider_deliveries(request):
    """
    Returns all orders accepted/assigned to the authenticated rider, 
    including the customer's delivery GPS coordinates for map rendering.
    """
    try:
        rider = await RiderProfile.objects.aget(user=request.user)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    def _get_deliveries(rider_obj):
        # Fetch orders assigned to this rider. 
        # Adjust the relation path (e.g. order.customer or order.marketplace_order.customer) based on your models.
        orders = LogisticOrder.objects.filter(assigned_rider=rider_obj).select_related('marketplace_order')
        
        deliveries_data = []
        for order in orders:
            # Assuming the order links to a user or customer profile with latitude/longitude
            customer = getattr(order, 'customer', None) or getattr(order.marketplace_order, 'customer', None)
            
            lat = float(customer.user.latitude) if customer and customer.user.latitude else None
            lng = float(customer.user.longitude) if customer and customer.user.longitude else None
            
            deliveries_data.append({
                "order_id": order.id,
                "status": order.status,
                "delivery_address": customer.user.street_address if customer else "N/A",
                "customer_name": f"{customer.first_name} {customer.last_name}" if customer else "Guest",
                "customer_phone": customer.user.phone_number if customer else "",
                "customer_latitude": lat,
                "customer_longitude": lng,
            })
        return deliveries_data

    deliveries = await sync_to_async(_get_deliveries, thread_sensitive=True)(rider)

    return {
        "status": "success",
        "deliveries": deliveries
    }

