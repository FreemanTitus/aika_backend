from typing import List, Optional
from pydantic import field_validator
from pydantic import BaseModel, Field
from datetime import datetime
from ninja import Schema
from decimal import Decimal

# =========================================================================
# 1. CORE INNER PAYLOAD SUB-SCHEMAS
# =========================================================================

class ProfileSummarySchema(Schema):
    """Serializes the core merchant profile configuration tracking variables."""
    company_name: str
    business_type: str
    is_approved: bool
    wallet_balance: float


class MetricSummarySchema(Schema):
    """Serializes aggregated business operation analytics data logs."""
    total_revenue: float
    orders_count: int
    success_rate: int
    active_deliveries: int
    pending_cod: float


class OrderRowSchema(Schema):
    """Structures individual data items mapped for the recent logs data grid."""
    id: int
    order_reference: str
    customer_name: str
    customer_phone: str
    total_amount: float
    status: str
    created_at: str


# =========================================================================
# 2. UNIFIED ROOT DATA ENVELOPE BUILDERS
# =========================================================================

class DashboardDataEnvelope(Schema):
    """Bundles distinct analytical and biographical streams into a single node."""
    business_profile: ProfileSummarySchema
    metrics: MetricSummarySchema
    recent_orders: List[OrderRowSchema]


class MerchantDashboardResponse(Schema):
    """
    The parent response schema injected into your Django-Ninja layout router.
    Guarantees consistent wire payload execution signatures.
    """
    status: str
    message: str
    data: Optional[DashboardDataEnvelope] = None

class AssignedRiderSchema(Schema):
    id: str
    name: str
    phone_number: str

class OrderItemSchema(Schema):
    name: str
    quantity: int
    price_at_order: float
    total_item_price: float

class MerchantOrderLogisticsSchema(Schema):
    id: int
    order_id: Optional[str] = None
    marketplace_order_id: Optional[str] = None
    order_reference: Optional[str] = None
    
    sender_name: Optional[str] = ""
    sender_phone: Optional[str] = ""
    pickup_address: Optional[str] = ""
    receiver_name: str
    receiver_phone: str
    delivery_address: str
    
    package_type: Optional[str] = None
    package_description: str
    items: List[OrderItemSchema] = []
    
    subtotal: float = 0.00
    delivery_fee: float = 0.00
    total_price: float = 0.00
    payment_reference: Optional[str] = None
    is_cash_on_delivery: bool = False
    cod_amount_to_collect: float = 0.00
    
    marketplace_status: Optional[str] = None
    status: str
    assigned_rider: Optional[AssignedRiderSchema] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

class ActiveOrdersListSchema(Schema):
    status: str
    active_orders: List[MerchantOrderLogisticsSchema]


class UpdateBusinessProfileInput(Schema):
    """
    Validates and unpacks incoming form parameters at the routing gateway.
    """
    company_name: str
    business_type: str
    phone_number: str
    opening_time: str  
    closing_time: str
    street_address: str
    base_location: Optional[int] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    ecommerce_sub_category: Optional[str] = None
    tagline: Optional[str] = None
    brand_bio: Optional[str] = None
    instagram_handle: Optional[str] = None
    twitter_handle: Optional[str] = None

class BusinessProfileResponse(Schema):
    """
    Type-safe structured response layout replacing legacy msgspec layouts.
    """
    id: str
    company_name: str
    business_type: str
    phone_number: str
    street_address: Optional[str] = None
    base_location: Optional[int] = None
    base_location_name: Optional[str] = None
    is_approved: bool
    is_currently_open: bool
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    ecommerce_sub_category: Optional[str] = None
    store_slug: Optional[str] = None
    tagline: Optional[str] = None
    brand_bio: Optional[str] = None
    instagram_handle: Optional[str] = None
    twitter_handle: Optional[str] = None
    hero_image: Optional[str] = None
    opening_time: str = ""
    closing_time: str = ""


class SubCategoryDetail(Schema):
    id: int
    name: str

class BrandDetail(Schema):
    id: int
    name: str

class VariantOptionDetail(Schema):
    name: str
    values: List[str]

class SKUItemDetail(Schema):
    id: int
    stock_qty: int
    price_override: Optional[Decimal] = None
    bonanza_mode: bool
    discount_price: Optional[Decimal] = None
    is_active: bool
    value_names: List[str]

class MenuOptionDetail(Schema):
    id: int
    name: str
    price: Decimal
    is_available: bool

class GalleryImageDetail(Schema):
    id: int
    image: str
    caption: str

class KeyFeatureItemDetail(Schema):
    title: str
    value: str

class ProductDetailResponseData(Schema):
    id: str
    name: str
    description: str
    key_features: List[KeyFeatureItemDetail]
    price: Decimal
    category: str
    sub_category: Optional[SubCategoryDetail] = None
    brand: Optional[BrandDetail] = None
    bonanza_mode: bool
    discount_price: Optional[Decimal] = None
    is_available: bool
    image: Optional[str] = None
    variant_options: List[VariantOptionDetail]
    sku_items: List[SKUItemDetail]
    menu_options: List[MenuOptionDetail] = []
    gallery_images: List[GalleryImageDetail]

class ProductDetailPayloadSchema(Schema):
    status: str = "success"
    data: ProductDetailResponseData

class CoordinateUpdatePayload(Schema):
    """
    Validates incoming floating point coordinates captured by client-side GPS
    or Autocomplete geocoding nodes.
    """
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    base_location: Optional[str] = None
    street_address: Optional[str] = None

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


class ShippingPreferenceInput(Schema):
    fulfillment_type: str = Field(..., pattern=r"^(platform|self|hybrid)$")
    allow_nationwide: bool = False
    self_delivery_fee: Decimal = Field(default=Decimal("0.00"), ge=0, le=Decimal("1000000"), decimal_places=2)


