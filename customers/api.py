import json
import math
import logging
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from channels.layers import get_channel_layer
from asgiref.sync import sync_to_async
from asgiref.sync import async_to_sync
from ninja import Router
from ninja.pagination import paginate, PageNumberPagination
from ninja.errors import HttpError
from django.db import transaction
# Core models and updated Pydantic schemas
from typing import List
from itertools import chain
from order.models import Order, UserCartItem
from .models import IndividualProfile, CustomerWallet, Referral
from accounts.utils import generate_alphanumeric_id, JWTAuth, create_jwt_for_user
from accounts.models import CoverageArea
from accounts.schemas import UserLoginSchema
from merchant.models import MerchantProfile, MerchantReview
from .schemas import (
    CoordinateUpdatePayload, MarketplaceOrderSchema, UnifiedBookingSchema,
    UserProfileOut, UserProfileUpdate, SubmitMerchantReviewSchema, CartItemInput,
    CartSyncPayload, ReferralResponseSchema
)
from rider.models import RiderWallet, RiderTransactionLedger
from booking.models import RideBooking, SendPackage
from marketplace.models import MarketplaceOrder
from audit.models import SystemAuditLog, OrderDisputeRefund
from audit.schemas import CustomerDisputeCreateSchema, CustomerDisputeOut
from decimal import Decimal, ROUND_HALF_UP
from django.utils.html import escape
from order.services import complete_marketplace_order
from wallet.utils import PAID_ORDER_STATES
from aika_platform.models import PlatformLedger
from rider.models import RiderProfile, RiderReview
from rider.schemas import SubmitRiderReviewSchema
from marketing.models import SystemBroadcast, UserNotification


logger = logging.getLogger(__name__)

router = Router(
    tags=["Individual Dashboard"], 
    auth=JWTAuth()
)

def _quantize_coord(value) -> Decimal:
    """Safely normalize any float/str/Decimal GPS coordinate to 6 decimal places."""
    return Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in kilometers between two GPS coordinates."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))

@router.get("/dashboard")
async def individual_dashboard(request):
    """
    Asynchronously retrieves the individual dashboard including 
    active ride bookings, package deliveries, active marketplace orders, and wallet/referral metrics.
    """
    user = request.user

    if user.role != 'individual':
        return JsonResponse({
            "status": "error", 
            "detail": "Access denied. Account is registered under an alternate profile tier."
        }, status=403)

    try:
        individual = await IndividualProfile.objects.aget(user=user)
    except IndividualProfile.DoesNotExist:
        return JsonResponse({
            "status": "error", 
            "detail": "Individual profile not found."
        }, status=404)

    # 1. Fetch or provision Customer Wallet asynchronously
    wallet, _ = await CustomerWallet.objects.aget_or_create(user=user, defaults={'balance': Decimal('0.00')})

    # 2. Count successful referrals and total invitations
    total_referrals = await Referral.objects.filter(inviter=user).acount()
    successful_rewards = await Referral.objects.filter(inviter=user, reward_granted=True).acount()

    # 3. Fetch Active Ride Bookings
    active_rides = await sync_to_async(list)(
        RideBooking.objects.filter(
            user=user, 
            status__in=['pending', 'assigned', 'in_transit']
        ).values('booking_reference', 'status', 'fare_amount', 'pickup_address', 'dropoff_address', 'created_at')
    )
    for ride in active_rides:
        ride['booking_type'] = 'ride'

    # 4. Fetch Active Package Deliveries
    active_packages = await sync_to_async(list)(
        SendPackage.objects.filter(
            user=user, 
            status__in=['pending', 'assigned', 'in_transit']
        ).values('booking_reference', 'status', 'fare_amount', 'pickup_address', 'dropoff_address', 'package_description', 'receiver_name', 'created_at')
    )
    for pkg in active_packages:
        pkg['booking_type'] = 'package'

    active_bookings = sorted(
        active_rides + active_packages, 
        key=lambda x: x['created_at'], 
        reverse=True
    )

    # 5. Fetch Active/Dispatched Marketplace Orders (excluding completed history)
    orders_qs = MarketplaceOrder.objects.filter(
        customer=individual
    ).exclude(
        status='completed'
    ).select_related(
        'vendor', 
        'logistics_dispatch',
        'logistics_dispatch__assigned_rider'
    ).order_by('-created_at')[:20]

    active_orders = []
    for order in await sync_to_async(list)(orders_qs):
        rider_data = None
        dispatch = getattr(order, 'logistics_dispatch', None)
        if dispatch and dispatch.assigned_rider:
            rider = dispatch.assigned_rider
            rider_data = {
                "id": rider.id,
                "full_name": rider.full_name,
                "bike_plate_number": rider.bike_plate_number
            }

        active_orders.append({
            "id": order.id,
            "total_price": str(order.total_price),
            "status": order.status,
            "created_at": order.created_at.isoformat(),
            "logistics_dispatch__status": dispatch.status if dispatch else None,
            "rider": rider_data,
            # Vendor-delivered orders have no waybill: these two keys drive the "confirm delivery" UI.
            "fulfillment_mode": order.fulfillment_mode,
            "vendor_marked_delivered": order.vendor_marked_delivered_at is not None,
        })
    
    return {
        "status": "success",
        "data": {
            "first_name": individual.first_name,
            "wallet_balance": float(wallet.balance),
            "referral": {
                "referral_code": individual.referral_code,
                "total_referrals": total_referrals,
                "successful_rewards": successful_rewards,
                "earned_bonus": float(wallet.balance)
            },
            "active_bookings": active_bookings,
            "order_history": active_orders # Keeps processing & active delivery confirmation items only
        }
    }

    
@router.get("/profile", response=UserProfileOut)
def get_customer_profile(request):
    """
    Retrieve the authenticated user's profile details safely across roles.
    Auto-provisions an IndividualProfile if it's missing for individual customers.
    """
    user = request.auth or request.user

    location_name = None
    if user.base_location_id:
        area = CoverageArea.objects.select_related('state').filter(id=user.base_location_id).first()
        if area:
            location_name = f"{area.name}, {area.state.name}"

    if getattr(user, 'role', None) == 'individual':
        try:
            profile = IndividualProfile.objects.select_related('user').get(user=user)
        except IndividualProfile.DoesNotExist:
            # Only real IndividualProfile fields are passed (phone_number / street_address
            # live on the user, not the profile, so the old create() always raised TypeError).
            placeholder_name = user.email.split('@')[0]
            try:
                profile = IndividualProfile.objects.create(
                    id=f"CUST-{generate_alphanumeric_id(8)}",
                    user=user,
                    first_name=placeholder_name.capitalize(),
                    last_name="",
                )
            except Exception:
                logger.exception("Failed auto-provisioning customer profile for user %s", user.id)
                raise HttpError(500, "Could not set up your profile. Please try again.")

        return {
            "status": "success",
            "data": {
                "id": str(profile.id),
                "email": user.email,
                "first_name": profile.first_name,
                "last_name": profile.last_name,
                "full_name": f"{profile.first_name} {profile.last_name}".strip(),
                "phone_number": user.phone_number or "Not Linked",
                "base_location": user.base_location_id,
                "base_location_name": location_name,
                "street_address": user.street_address or profile.default_delivery_address or "",
                "created_at": profile.created_at.isoformat(),
                "coordinates": {
                    "latitude": float(user.latitude) if user.latitude else None,
                    "longitude": float(user.longitude) if user.longitude else None
                }
            }
        }

    # Fallback response for non-customer roles (Admins, Merchants, Business, etc.)
    return {
        "status": "success",
        "data": {
            "id": str(user.id),
            "email": user.email,
            "first_name": user.first_name or 'Admin/Staff',
            "last_name": user.last_name or '',
            "phone_number": user.phone_number or 'Not Linked',
            "base_location": user.base_location_id,
            "base_location_name": location_name,
            "street_address": user.street_address or "",
            "created_at": user.date_joined.isoformat() if user.date_joined else None,
            "coordinates": {
                "latitude": float(user.latitude) if user.latitude else None,
                "longitude": float(user.longitude) if user.longitude else None
            }
        }
    }

@router.patch("/profile", response=UserProfileOut)
def update_customer_profile(request, data: UserProfileUpdate):
    """
    Update the authenticated customer's profile details and location hub safely.
    """
    user = request.auth or request.user
    
    try:
        profile = IndividualProfile.objects.select_related('user').get(user=user)
    except IndividualProfile.DoesNotExist:
        # Auto-provision if missing
        placeholder_name = user.email.split('@')[0]
        profile = IndividualProfile.objects.create(
            id=f"CUST-{generate_alphanumeric_id(8)}",
            user=user,
            first_name=placeholder_name.capitalize(),
            last_name=""
        )

    if data.first_name is not None:
        profile.first_name = data.first_name
    if data.last_name is not None:
        profile.last_name = data.last_name
    if data.street_address is not None:
        if hasattr(profile, 'default_delivery_address'):
            profile.default_delivery_address = data.street_address
        elif hasattr(profile, 'street_address'):
            profile.street_address = data.street_address

    # Handle Foreign Key update for base_location on the CustomUser model
    if data.base_location is not None:
        try:
            area = CoverageArea.objects.get(id=data.base_location)
            user.base_location = area
            user.save(update_fields=['base_location'])
        except CoverageArea.DoesNotExist:
            pass

    profile.save()

    # Resolve the human-readable location name safely
    location_name = None
    if user.base_location_id:
        try:
            area = CoverageArea.objects.select_related('state').get(id=user.base_location_id)
            location_name = f"{area.name}, {area.state.name}"
        except CoverageArea.DoesNotExist:
            pass

    return {
        "status": "success",
        "data": {
            "id": str(profile.id),
            "email": user.email,
            "first_name": profile.first_name,
            "last_name": profile.last_name,
            "phone_number": getattr(user, 'phone_number', None) or "Not Linked",
            "base_location": user.base_location_id,
            "base_location_name": location_name,
            "street_address": getattr(profile, 'default_delivery_address', getattr(user, 'street_address', None)) or "",
            "created_at": profile.created_at.isoformat(),
            "coordinates": {
                "latitude": float(user.latitude) if user.latitude else None,
                "longitude": float(user.longitude) if user.longitude else None
            }
        }
    }

@router.get("/orders", response=List[MarketplaceOrderSchema])
@paginate(PageNumberPagination)
def list_customer_orders(request):
    if request.user.role != 'individual':
        raise HttpError(403, "Access restricted.")
    
    try:
        individual = IndividualProfile.objects.get(user=request.user)
    except IndividualProfile.DoesNotExist:
        raise HttpError(404, "Profile not found.")

    return MarketplaceOrder.objects.filter(
        customer=individual
    ).select_related(
        'vendor', 
        'logistics_dispatch', 
        'logistics_dispatch__assigned_rider'
    ).order_by('-created_at')

@router.get("/bookings-list", response=List[UnifiedBookingSchema])
def list_customer_bookings(request):
    if request.user.role != 'individual':
        raise HttpError(403, "Access restricted.")

    user = request.user

    # Fetch querysets directly as lists synchronously
    rides = list(
        RideBooking.objects.filter(user=user).values(
            'booking_reference', 'status', 'fare_amount', 'pickup_address', 'dropoff_address', 'created_at'
        )
    )
    for r in rides:
        r['booking_type'] = 'ride'
        r['receiver_name'] = None
        r['package_description'] = None

    packages = list(
        SendPackage.objects.filter(user=user).values(
            'booking_reference', 'status', 'fare_amount', 'pickup_address', 'dropoff_address', 'receiver_name', 'package_description', 'created_at'
        )
    )
    for p in packages:
        p['booking_type'] = 'package'

    # Merge and sort descending by creation date
    all_bookings = sorted(
        chain(rides, packages), 
        key=lambda x: x['created_at'], 
        reverse=True
    )

    return all_bookings

@router.post("/profile/update-coordinates")
def update_user_coordinates_api(request, data: CoordinateUpdatePayload):
    """
    Synchronously links real-time device coordinate geometries
    and address metadata to the customer's user record and IndividualProfile.
    """
    user = request.user

    if not user or user.is_anonymous:
        raise HttpError(401, "Authentication credentials missing or invalid.")

    try:
        profile, _ = IndividualProfile.objects.get_or_create(
            user=user,
            defaults={
                "first_name": user.first_name or "Customer",
                "last_name": user.last_name or "User",
            },
        )

        update_fields = []

        if data.street_address is not None:
            clean_address = data.street_address.strip()
            profile.default_delivery_address = clean_address
            profile.save(update_fields=["default_delivery_address"])
            user.street_address = clean_address
            update_fields.append("street_address")

        # Safely resolve CoverageArea whether payload sends an int ID or string name
        target_coverage = user.base_location
        if data.base_location is not None:
            if isinstance(data.base_location, int) or str(data.base_location).isdigit():
                target_coverage = CoverageArea.objects.filter(id=int(data.base_location)).first()
            else:
                target_coverage = CoverageArea.objects.filter(
                    name__iexact=str(data.base_location).strip()
                ).first()

            if not target_coverage:
                raise HttpError(400, f"Invalid base location: '{data.base_location}'.")

            user.base_location = target_coverage
            update_fields.append("base_location")

        if data.latitude is not None and data.longitude is not None:
            lat_val = float(data.latitude)
            lng_val = float(data.longitude)

            if not (-90.0 <= lat_val <= 90.0 and -180.0 <= lng_val <= 180.0):
                raise HttpError(400, "Invalid GPS coordinates provided.")

            # Guard against emulator/VPN defaults (0,0) or coordinates far outside the hub region (> 50km)
            if (
                target_coverage
                and target_coverage.latitude is not None
                and target_coverage.longitude is not None
            ):
                dist_km = _haversine_km(
                    float(target_coverage.latitude),
                    float(target_coverage.longitude),
                    lat_val,
                    lng_val,
                )
                if dist_km > 50.0:
                    raise HttpError(
                        400,
                        f"Your device GPS ({lat_val:.4f}, {lng_val:.4f}) is outside your selected hub ({target_coverage.name}).",
                    )

            user.latitude = _quantize_coord(lat_val)
            user.longitude = _quantize_coord(lng_val)
            update_fields.extend(["latitude", "longitude"])

        if update_fields:
            user.save(update_fields=update_fields)

        return {
            "status": "success",
            "message": "Geospatial metrics synchronized successfully.",
            "data": {
                "latitude": float(user.latitude) if user.latitude is not None else None,
                "longitude": float(user.longitude) if user.longitude is not None else None,
                "base_location": user.base_location.name if user.base_location else "",
                "street_address": user.street_address,
            },
        }

    except HttpError:
        raise
    except Exception as e:
        logger.exception("Failed updating coordinates for user %s", user.id)
        raise HttpError(500, f"An unhandled database mutation fault occurred: {str(e)}")


@router.get("/coordinates")
def get_customer_coordinates_api(request):
    """
    Retrieves the real-time device coordinate geometries
    and address metadata for the authenticated customer synchronously.
    """
    user = request.user

    if not user or user.is_anonymous:
        raise HttpError(401, "Authentication credentials missing or invalid.")

    profile = (
        IndividualProfile.objects.select_related("user", "user__base_location")
        .filter(user=user)
        .first()
    )

    # Prefer live user coordinates, falling back to profile or hub coordinates
    lat = user.latitude if user.latitude is not None else getattr(profile, "latitude", None)
    lng = user.longitude if user.longitude is not None else getattr(profile, "longitude", None)

    if (lat is None or lng is None) and user.base_location:
        lat = user.base_location.latitude
        lng = user.base_location.longitude

    base_location_name = user.base_location.name if user.base_location else ""
    street_addr = (
        (profile.default_delivery_address if profile and profile.default_delivery_address else None)
        or user.street_address
    )

    return {
        "status": "success",
        "data": {
            "latitude": float(lat) if lat is not None else None,
            "longitude": float(lng) if lng is not None else None,
            "base_location": base_location_name,
            "street_address": street_addr,
        },
    }

@router.post("/orders/{marketplace_id}/complete", response={200: dict})
async def customer_mark_order_completed(request, marketplace_id: str):
    if request.user.role != 'individual':
        raise HttpError(403, "Access restricted to individual accounts.")

    # All money movement (rider credit, platform cut, completion) lives in one locked,
    # idempotent service shared with the order confirm-delivery endpoint: two taps or two
    # concurrent requests can no longer pay the rider twice.
    try:
        m_order, logistics_order, rider_new_balance, rider_user_id = await sync_to_async(
            complete_marketplace_order, thread_sensitive=True
        )(marketplace_id, request.user)
    except LookupError:
        raise HttpError(404, "Marketplace order not found or unauthorized.")
    except ValueError as e:
        raise HttpError(400, str(e))

    # --- Real-time notifications (never fail the request) ---
    try:
        order_id_ref = str(m_order.id)
        channel_layer = get_channel_layer()
        if channel_layer:
            # Dynamically extract rider earning from the logistics record if available
            rider_share = logistics_order.rider_earning if logistics_order else Decimal('0.00')

            await channel_layer.group_send(
                "admin_notifications",
                {
                    "type": "order_completed_alert",
                    "payload": {
                        "order_reference": order_id_ref,
                        "message": (
                            f"Customer confirmed Order #{order_id_ref}. Rider credited with ₦{rider_share:,.2f}."
                            if logistics_order is not None
                            else f"Customer confirmed vendor-delivered Order #{order_id_ref}."
                        ),
                    },
                },
            )
            # Vendor dashboards listen on vendor_<MerchantProfile.id> (not the user id).
            if m_order.vendor_id:
                await channel_layer.group_send(
                    f"vendor_{m_order.vendor_id}",
                    {
                        "type": "order_completed_alert",
                        "payload": {
                            "message": f"Order #{order_id_ref} has been marked as completed by the customer.",
                        },
                    },
                )
            if rider_user_id:
                await channel_layer.group_send(
                    f"rider_{rider_user_id}",
                    {
                        "type": "wallet_update_alert",
                        "payload": {
                            "message": f"Order #{order_id_ref} completed! ₦{rider_share:,.2f} added.",
                            "new_balance": rider_new_balance,
                        },
                    },
                )
    except Exception:
        logger.exception("Failed to broadcast order completion alerts for order %s", marketplace_id)

    if logistics_order is None:
        return {"status": "success", "message": "Order completed. Thank you for confirming your delivery!"}

    return {
        "status": "success",
        "message": (
            f"Order completed, rider credited with ₦{logistics_order.rider_earning:,.2f} and "
            f"₦{logistics_order.platform_fee:,.2f} retained by platform."
        ),
    }

@router.post("/merchant/{merchant_id}/review")
def submit_merchant_review(request, merchant_id: str, payload: SubmitMerchantReviewSchema):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication required to leave a review.")

    try:
        merchant = MerchantProfile.objects.get(id=merchant_id)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Merchant profile not found.")

    if merchant.user_id == user.id:
        raise HttpError(403, "You cannot review your own store.")

    review, created = MerchantReview.objects.update_or_create(
        merchant=merchant,
        customer=user,
        defaults={
            'rating': payload.rating,
            'comment': payload.comment
        }
    )

    # The aggregate rating is recalculated by the review signal: reload it, otherwise the
    # response reports the stale pre-review average.
    merchant.refresh_from_db(fields=['rating', 'review_count'])

    action_text = "submitted" if created else "updated"
    return {
        "status": "success",
        "message": f"Review successfully {action_text}.",
        "new_average_rating": float(merchant.rating),
        "total_reviews": merchant.review_count
    }

@router.get("/disputes", response=List[CustomerDisputeOut])
def list_customer_disputes(request):
    """Lists disputes for orders that belong to the logged-in customer."""
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication required.")

    # Ownership is derived from the ORDER, never from the display name (two customers can
    # share a name, which used to expose one customer's disputes to the other).
    own_order_ids = MarketplaceOrder.objects.filter(customer__user=user).values('id')
    disputes = OrderDisputeRefund.objects.filter(order_id__in=own_order_ids).order_by('-created_at')

    return [
        {
            "id": d.id,
            "order_id": d.order_id,
            "reason": d.reason,
            "amount": float(d.amount),
            "status": d.status,
            "created_at": d.created_at,
        } for d in disputes
    ]

@router.post("/disputes/create", response=CustomerDisputeOut)
async def create_customer_dispute(request, payload: CustomerDisputeCreateSchema):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication required.")

    try:
        claimed_amount = Decimal(str(payload.amount)).quantize(Decimal("0.01"))
    except Exception:
        raise HttpError(400, "Invalid refund amount.")
    if claimed_amount <= 0:
        raise HttpError(400, "Refund amount must be greater than zero.")

    account_number = (payload.account_number or "").strip()
    if not (account_number.isdigit() and len(account_number) == 10):
        raise HttpError(400, "A valid 10-digit account number is required.")

    # Ownership: a customer can only open a dispute on their own PAID order, for at most
    # what the order cost, and only one open dispute per order. (Previously any user could
    # file a refund claim of any amount against any order id, paid out to any bank account.)
    order = await MarketplaceOrder.objects.filter(
        id=payload.order_id, customer__user=user
    ).afirst()
    if order is None:
        raise HttpError(404, "Order not found.")
    if order.status not in PAID_ORDER_STATES:
        raise HttpError(400, "Only paid orders can be disputed.")
    if claimed_amount > order.total_price:
        raise HttpError(400, "The refund amount cannot exceed the order total.")
    if await OrderDisputeRefund.objects.filter(
        order_id=order.id, status=OrderDisputeRefund.StatusChoice.PENDING
    ).aexists():
        raise HttpError(409, "A dispute for this order is already under review.")

    customer_identifier = user.get_full_name() or user.email

    dispute = await OrderDisputeRefund.objects.acreate(
        order_id=order.id,
        customer_name=customer_identifier,
        reason=payload.reason.strip()[:2000],
        amount=claimed_amount,
        bank_name=(payload.bank_name or "").strip()[:100],
        account_number=account_number,
        account_name=(payload.account_name or "").strip()[:150],
        status=OrderDisputeRefund.StatusChoice.PENDING,
        raised_by='Customer'
    )

    # BROADCAST REAL-TIME NOTIFICATION TO ADMINS ASYNCHRONOUSLY
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            await channel_layer.group_send(
                "admin_notifications",
                {
                    "type": "admin_notification_message",
                    "message": {
                        "title": "New Dispute Claim Lodged",
                        "text": (
                            f"Customer <b>{escape(dispute.customer_name)}</b> filed a claim of "
                            f"<b>\u20a6{dispute.amount:,.2f}</b> for Order <b>#{escape(dispute.order_id)}</b>."
                        ),
                        "dispute_id": dispute.id,
                        "created_at": dispute.created_at.isoformat()
                    }
                }
            )
    except Exception:
        logger.exception("Failed to broadcast dispute notification for dispute %s", dispute.id)

    return {
        "id": dispute.id,
        "order_id": dispute.order_id,
        "reason": dispute.reason,
        "amount": float(dispute.amount),
        "status": dispute.status,
        "created_at": dispute.created_at,
    }

@router.post("/rider/{rider_id}/review")
def submit_rider_review(request, rider_id: str, payload: SubmitRiderReviewSchema):
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Authentication required to leave a review.")

    try:
        rider = RiderProfile.objects.get(id=rider_id)
    except RiderProfile.DoesNotExist:
        raise HttpError(404, "Rider profile not found.")

    if rider.user_id == user.id:
        raise HttpError(403, "You cannot review yourself.")

    review, created = RiderReview.objects.update_or_create(
        rider=rider,
        customer=user,
        defaults={
            'rating': payload.rating,
            'comment': payload.comment
        }
    )

    rider.refresh_from_db(fields=['rating', 'review_count'])

    action_text = "submitted" if created else "updated"
    return {
        "status": "success",
        "message": f"Rider review successfully {action_text}.",
        "new_average_rating": float(rider.rating),
        "total_reviews": rider.review_count
    }

@router.get("/cart")
def get_customer_cart_api(request):
    """Retrieves saved cart items for the authenticated user synchronously."""
    user = request.user
    cart_items = [
        {
            "id": item.product_id,
            "product_id": item.product_id,
            "quantity": item.quantity,
            "storeId": item.store_id,
            "name": item.name,
            "price": float(item.price),
            "vendorName": item.vendor_name
        }
        for item in UserCartItem.objects.filter(user=user)
    ]
    return {"status": "success", "data": cart_items}

@router.post("/cart/sync")
def sync_customer_cart_api(request, data: CartSyncPayload):
    """Upserts and syncs user cart items in a constant number of queries."""
    user = request.user

    if len(data.items) > 200:
        raise HttpError(400, "Too many cart items.")

    desired = {}      # product_id -> item (last occurrence wins)
    to_remove = set()
    for item in data.items:
        pid = str(item.product_id)[:100]
        if item.quantity > 0:
            desired[pid] = item
            to_remove.discard(pid)
        else:
            to_remove.add(pid)
            desired.pop(pid, None)

    with transaction.atomic():
        if to_remove:
            UserCartItem.objects.filter(user=user, product_id__in=to_remove).delete()

        if desired:
            existing = {row.product_id: row for row in UserCartItem.objects.filter(user=user, product_id__in=desired.keys())}
            to_update, to_create = [], []
            for pid, item in desired.items():
                values = dict(
                    quantity=min(int(item.quantity), 999),
                    store_id=item.store_id,
                    name=(item.name or '')[:255] or None,
                    price=Decimal(str(item.price or 0)),
                    vendor_name=(item.vendor_name or '')[:255] or None,
                )
                row = existing.get(pid)
                if row:
                    for field, value in values.items():
                        setattr(row, field, value)
                    to_update.append(row)
                else:
                    to_create.append(UserCartItem(user=user, product_id=pid, **values))
            if to_update:
                UserCartItem.objects.bulk_update(
                    to_update, ['quantity', 'store_id', 'name', 'price', 'vendor_name', 'updated_at']
                )
            if to_create:
                UserCartItem.objects.bulk_create(to_create)

    return {"status": "success", "message": "Cart synchronized successfully."}

@router.get("/referrals", response=ReferralResponseSchema)
def get_customer_referrals(request):
    """Retrieves referral code and statistics summary synchronously."""
    user = request.user

    try:
        individual = IndividualProfile.objects.get(user=user)
        referral_code = getattr(individual, 'referral_code', f"AIKA-{user.id}")
    except IndividualProfile.DoesNotExist:
        referral_code = f"AIKA-{user.id}"

    referrals_list = list(Referral.objects.filter(inviter=user).select_related('invitee').order_by('-created_at'))

    total_referrals = len(referrals_list)
    successful_rewards = sum(1 for r in referrals_list if r.reward_granted)
    pending_rewards = total_referrals - successful_rewards

    formatted_referrals = [
        {
            "invitee_name": f"{ref.invitee.first_name} {ref.invitee.last_name}".strip() or ref.invitee.email.split('@')[0],
            "invitee_email": ref.invitee.email,
            "has_completed_qualifying_order": ref.has_completed_qualifying_order,
            "reward_granted": ref.reward_granted,
            "created_at": ref.created_at
        }
        for ref in referrals_list
    ]

    return {
        "status": "success",
        "data": {
            "referral_code": referral_code,
            "total_referrals": total_referrals,
            "successful_rewards": successful_rewards,
            "pending_rewards": pending_rewards,
            "referrals": formatted_referrals
        }
    }

@router.get("/notifications", response={200: list})
def get_customer_notifications(request):
    """Fetch system broadcasts and personal notifications synchronously."""
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication required.")

    broadcasts = SystemBroadcast.objects.filter(
        target_audience__in=['all', 'customers']
    ).order_by('-created_at')[:10]

    personal_notifications = UserNotification.objects.filter(
        user=user
    ).order_by('-created_at')[:15]

    notification_list = []

    for b in broadcasts:
        notification_list.append({
            "id": f"broadcast-{b.id}",
            "type": "broadcast",
            "message": f"<b>{b.title}</b><br/><span class='text-gray-600 font-normal'>{b.message}</span>",
            "timestamp": b.created_at.strftime("%H:%M - %b %d"),
            "read": False,
            "raw_id": b.id
        })

    for n in personal_notifications:
        notification_list.append({
            "id": f"user-notif-{n.id}",
            "type": n.notification_type,
            "message": f"<b>{n.title}</b><br/><span class='text-gray-600 font-normal'>{n.message}</span>",
            "timestamp": n.created_at.strftime("%H:%M - %b %d"),
            "read": n.is_read,
            "raw_id": n.id
        })

    return notification_list

@router.post("/notifications/mark-read/{notif_type}/{notif_id}", response={200: dict})
def mark_notification_as_read(request, notif_type: str, notif_id: int):
    """Mark broadcast or personal notification as read synchronously."""
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication required.")

    if notif_type == "broadcast":
        try:
            broadcast = SystemBroadcast.objects.get(id=notif_id)
        except SystemBroadcast.DoesNotExist:
            raise HttpError(404, "Broadcast not found.")
        return {"status": "success", "message": "Broadcast acknowledged."}
    else:
        try:
            notification = UserNotification.objects.get(id=notif_id, user=user)
        except UserNotification.DoesNotExist:
            raise HttpError(404, "Notification not found.")
            
        notification.is_read = True
        notification.save(update_fields=['is_read'])
        return {"status": "success", "message": "Notification marked as read."}
    
@router.post("/customer/notifications/mark-all-read", response={200: dict})
async def mark_all_notifications_as_read(request):
    """
    Mark all unread personal notifications as read asynchronously.
    """
    user = getattr(request, 'auth', None) or getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        raise HttpError(403, "Authentication required.")

    await UserNotification.objects.filter(user=user, is_read=False).aupdate(is_read=True)
    return {"status": "success", "message": "All notifications marked as read."}

@router.get("/notifications/unread-count")
async def get_unread_notification_count(request):
    if not request.user.is_authenticated:
        return {"unread_count": 0}
    count = await UserNotification.objects.filter(user=request.user, is_read=False).acount()
    return {"unread_count": count}

@router.post("/notifications/{notification_id}/mark-read")
async def mark_single_notification_read(request, notification_id: int):
    if not request.user.is_authenticated:
        raise HttpError(401, "Unauthorized")
    try:
        notification = await UserNotification.objects.aget(id=notification_id, user=request.user)
        notification.is_read = True
        await notification.asave(update_fields=['is_read'])
    except UserNotification.DoesNotExist:
        raise HttpError(404, "Notification not found")
    return {"status": "success"}

@router.post("/notifications/mark-all-read")
async def mark_all_notifications_read_alias(request):
    if not request.user.is_authenticated:
        raise HttpError(401, "Unauthorized")
    
    # Bulk update all unread notifications for this user
    await UserNotification.objects.filter(user=request.user, is_read=False).aupdate(is_read=True)
    return {"status": "success"}