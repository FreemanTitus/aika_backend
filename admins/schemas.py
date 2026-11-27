from ninja import Schema
from pydantic import BaseModel
from typing import List, Optional, Any, Literal
from datetime import datetime
from pydantic import EmailStr, field_validator, model_validator
from decimal import Decimal


BillingIntervalLiteral = Literal["monthly", "quarterly", "semi_annual", "annual"]

class DispatchOrderSchema(Schema):
    id: str
    status: str
    updated_at: datetime
    sender_name: Optional[str] = None  # Adjust based on your sender model fields

class AvailableRiderSchema(Schema):
    id: str
    first_name: str
    last_name: str
    bike_plate_number: str
    status: str
    rating: float


class AdminDashboardResponseSchema(Schema):
    pending_dispatches: List[DispatchOrderSchema]
    available_riders: List[AvailableRiderSchema]


class DashboardMetricsSchema(Schema):
    total_orders: int
    total_merchants: int
    total_riders: int
    total_customers: int
    pending_dispatches_count: int
    available_riders_count: int
    completed_deliveries_count: int


class ChartTrendsSchema(Schema):
    days_labels: List[str]
    days_revenue: List[float]
    days_volumes: List[int]


class AdminDashboardStatsSchema(Schema):
    metrics: DashboardMetricsSchema
    chart_trends: ChartTrendsSchema


class ProfitBreakdownSchema(Schema):
    total_revenue: float
    delivery_profit: float
    subscription_profit: float
    platform_commission_profit: float


class AdminTransactionSchema(Schema):
    transaction_reference: str
    source_type: str  # 'delivery', 'subscription', or 'marketplace_commission'
    amount: float
    description: str
    created_at: datetime


class AdminProfitResponseSchema(Schema):
    summary: ProfitBreakdownSchema
    transactions: List[AdminTransactionSchema]

    
class UserOut(Schema):
    id: Any
    email: str
    first_name: str
    last_name: str


class VendorOut(Schema):
    id: Any
    company_name: str
    business_type: Optional[str]
    is_active_store: bool
    is_approved: bool
    user: UserOut
    assigned_agent: Optional[UserOut] = None


class SingleVendorResponseOut(Schema):
    vendor: VendorOut
    staff_agents: List[UserOut]


class AdminVendorDashboardOut(BaseModel):
    vendors: List[VendorOut]
    current_filter: Optional[str] = None
    staff_agents: List[UserOut]


class AssignAgentSchema(Schema):
    assigned_agent_id: Optional[Any] = None


class BaseStaffOnboardingInput(Schema):
    email: EmailStr
    password: str
    phone_number: str
    base_location: str
    street_address: Optional[str] = None
    is_terms_accepted: bool

    @staticmethod
    def _strip(value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        if isinstance(value, str):
            return value.strip().lower()
        return value
    
    @field_validator("is_terms_accepted")
    @classmethod
    def validate_terms(cls, value):
        if not value:
            raise ValueError("Terms must be accepted.")
        return value
    
    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return value
    
    @field_validator("phone_number", mode="before")
    @classmethod
    def strip_phone_number(cls, value):
        return cls._strip(value)
    
    @field_validator("street_address", mode="before")
    @classmethod
    def normalize_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class StaffOnboardingInput(BaseStaffOnboardingInput):
    account_role: Literal['admin', 'staff', 'agent'] = 'staff'
    first_name: str
    last_name: str
    phone_number: str
    base_location: int
    street_address: Optional[str] = ""
    department: Optional[str] = ""
    account_role: str
    is_terms_accepted: bool = True

    @field_validator("first_name", "last_name", "department", mode="before")
    @classmethod
    def strip_names(cls, value):
        return cls._strip(value)


class StaffOnboardingResponse(Schema):
    status: str
    message: str
    user_id: Any
    profile_id: Optional[str] = None


class StaffProfileOut(Schema):
    department: Optional[str] = None
    is_available_for_assignment: bool
    max_merchant_capacity: int


class AgentProfileOut(Schema):
    is_available_for_assignment: bool
    is_online: bool
    max_merchant_capacity: int

class StaffUserOut(Schema):
    id: Any
    email: str
    first_name: str
    last_name: str
    phone_number: str
    base_location: str
    street_address: Optional[str] = None
    role: str
    staff_profile: Optional[StaffProfileOut] = None

class AgentUserOut(Schema):
    id: Any
    email: str
    first_name: str
    last_name: str
    phone_number: str
    base_location: str
    street_address: Optional[str] = None
    role: str
    staff_profile: Optional[AgentProfileOut] = None

class StaffListResponseOut(Schema):
    staff_members: List[StaffUserOut]

class AgentListResponseOut(Schema):
    agent_members: List[AgentUserOut]

class ToggleApprovalSchema(Schema):
    action: str


class SubscriptionPlanCreateSchema(Schema):
    name: str
    billing_interval: BillingIntervalLiteral = "monthly"
    price: float
    description: Optional[str] = None
    features_raw: str
    max_monthly_orders: Optional[int] = None
    max_products: Optional[int] = None
    has_priority_dispatch: bool = False
    has_featured_placement: bool = False
    has_advanced_analytics: bool = False
    is_available: Optional[bool] = True


class SubscriptionPlanOutSchema(Schema):
    id: int
    name: str
    slug: str
    billing_interval: str
    price: float
    description: Optional[str] = None
    max_monthly_orders: Optional[int] = None
    max_products: Optional[int] = None
    has_priority_dispatch: bool
    has_featured_placement: bool
    has_advanced_analytics: bool
    features_raw: str
    features_list: List[str]
    is_available: bool


class SubscriptionPlanUpdateSchema(Schema):
    name: Optional[str] = None
    billing_interval: Optional[BillingIntervalLiteral] = None
    price: Optional[float] = None
    description: Optional[str] = None
    features_raw: Optional[str] = None
    max_monthly_orders: Optional[int] = None
    max_products: Optional[int] = None
    has_priority_dispatch: Optional[bool] = None
    has_featured_placement: Optional[bool] = None
    has_advanced_analytics: Optional[bool] = None
    is_available: Optional[bool] = None

CategoryLiteral = Literal[
    "gadgets",
    "apparel",
    "appliances",
    "beauty",
    "meals",
    "drinks",
    "sides",
    "general",
]


class ProductUpdateSchema(Schema):
    """
    Validates inventory updates for Restaurants, Pharmacies, and Retailers 
    using optional fields to support partial PUT/PATCH requests.
    """
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    category: Optional[str] = None  # Or CategoryLiteral if defined

    key_features: Optional[str] = None
    
    sub_category_id: Optional[str] = None
    new_sub_category_name: Optional[str] = None

    # Brand Controls
    brand_selection: Optional[str] = None                      
    new_brand_name: Optional[str] = None       
    
    # Promotional Rulesets
    bonanza_mode: Optional[bool] = None
    discount_price: Optional[Decimal] = None
    is_available: Optional[bool] = None

    image_key: Optional[str] = None
    
    # Complex payloads / JSON structures
    variant_options: Optional[Any] = None
    sku_items: Optional[Any] = None
    gallery_images: Optional[Any] = None


class RiderPayoutAuditOut(Schema):
    id: str
    rider_name: str
    phone: Optional[str] = None
    bike_plate_number: Optional[str] = None
    payout_schedule: str
    payout_method: str
    wallet_balance: float
    total_deliveries: int
    is_active: bool

class LocationCreateInput(Schema):
    state_name: str
    area_name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class LocationResponse(Schema):
    id: int
    name: str
    state_name: str


class PlatformSettingCreateSchema(Schema):
    referral_order_threshold: float
    referral_bonus_amount: float

class PlatformSettingOutSchema(Schema):
    referral_order_threshold: float
    referral_bonus_amount: float

class DeliveryConfigOut(Schema):
    full_time_rider_earning: Decimal
    casual_platform_percentage: Decimal
    updated_at: str

class DeliveryConfigUpdateIn(Schema):
    full_time_rider_earning: Decimal = None
    casual_platform_percentage: Decimal = None

    