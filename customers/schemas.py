from ninja import Schema
from pydantic import Field, field_validator
from decimal import Decimal
from typing import Optional, List, Union
from datetime import datetime


class UserProfileDataSchema(Schema):
    id: str
    email: str
    first_name: str
    last_name: str
    phone_number: Optional[str] = None
    base_location: Optional[int] = None
    base_location_name: Optional[str] = None
    street_address: Optional[str] = None
    coordinates: Optional[dict] = None
    created_at: Optional[str] = None

class UserProfileOut(Schema):
    status: str
    data: UserProfileDataSchema


class UserProfileUpdate(Schema):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    street_address: Optional[str] = None
    base_location: Optional[int] = None
    phone_number: Optional[str] = None

class CoordinateUpdatePayload(Schema):
    """
    Validates incoming floating point coordinates captured by client-side GPS
    or Autocomplete geocoding nodes.
    """
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    base_location: Optional[Union[int, str]] = None
    street_address: Optional[str] = None

    @field_validator('base_location')
    @classmethod
    def normalize_base_location(cls, v: Optional[Union[int, str]]) -> Optional[Union[int, str]]:
        if v is None:
            return None
        if isinstance(v, str):
            stripped = v.strip()
            return stripped if stripped else None
        return v

    @field_validator('latitude')
    @classmethod
    def validate_latitude_bounds(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return None
        if v < Decimal('-90.0') or v > Decimal('90.0'):
            raise ValueError('Latitude must range strictly between -90.0 and 90.0 degrees.')
        return v

    @field_validator('longitude')
    @classmethod
    def validate_longitude_bounds(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return None
        if v < Decimal('-180.0') or v > Decimal('180.0'):
            raise ValueError('Longitude must range strictly between -180.0 and 180.0 degrees.')
        return v

class VendorInfoSchema(Schema):
    id: str
    company_name: str


class RiderInfoSchema(Schema):
    id: str
    full_name: str
    bike_plate_number: Optional[str] = None

class MarketplaceOrderSchema(Schema):
    id: str
    total_price: float
    status: str
    created_at: datetime
    logistics_dispatch__status: Optional[str] = None
    vendor: VendorInfoSchema
    rider: Optional[RiderInfoSchema] = None

    @staticmethod
    def resolve_logistics_dispatch__status(obj):
        if obj.logistics_dispatch:
            return obj.logistics_dispatch.status
        return None

    @staticmethod
    def resolve_rider(obj):
        # Access assigned_rider from the logistics_dispatch relation
        if obj.logistics_dispatch and obj.logistics_dispatch.assigned_rider:
            rider = obj.logistics_dispatch.assigned_rider
            return {
                "id": rider.id,
                "full_name": rider.full_name,
                "bike_plate_number": rider.bike_plate_number
            }
        return None


class UnifiedBookingSchema(Schema):
    booking_reference: str
    booking_type: str  # 'ride' or 'package'
    status: str
    fare_amount: float
    pickup_address: str
    dropoff_address: str
    created_at: datetime
    # Optional package-specific fields
    receiver_name: Optional[str] = None
    package_description: Optional[str] = None

class FAQSchema(Schema):
    question: str
    answer: str
    order: int = 0
    is_active: bool = True


class SubmitMerchantReviewSchema(Schema):
    rating: int = Field(..., ge=1, le=5, description="Star rating between 1 and 5")
    comment: Optional[str] = Field(default=None, max_length=500, description="Optional written review comment")

class CartItemInput(Schema):
    product_id: str
    quantity: int
    store_id: Optional[str] = None
    name: Optional[str] = None
    price: Optional[float] = 0.00
    vendor_name: Optional[str] = None

class CartSyncPayload(Schema):
    items: List[CartItemInput]


class ReferralItemSchema(Schema):
    invitee_name: str
    invitee_email: str
    has_completed_qualifying_order: bool
    reward_granted: bool
    created_at: datetime

class ReferralSummarySchema(Schema):
    referral_code: str
    total_referrals: int
    successful_rewards: int
    pending_rewards: int
    referrals: List[ReferralItemSchema]

class ReferralResponseSchema(Schema):
    status: str
    data: ReferralSummarySchema