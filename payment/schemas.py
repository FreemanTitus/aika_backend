from datetime import datetime

from ninja import Router, Schema
from typing import Optional, Any, List, Literal



BillingIntervalLiteral = Literal["monthly", "quarterly", "semi_annual", "annual"]


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

class SubscriptionPlanOut(Schema):
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

class SubscriptionInvoiceOut(Schema):
    id: int
    reference_id: str
    amount: float
    status: str
    payment_date: Optional[datetime] = None
    plan: Optional[SubscriptionPlanOut] = None

class SubscriptionDashboardOut(Schema):
    has_access: bool
    current_plan: Optional[SubscriptionPlanOut] = None
    next_billing_date: Optional[datetime] = None
    platform_plans: List[SubscriptionPlanOut]
    invoices: List[SubscriptionInvoiceOut]


class OPayCheckoutResponseOut(Schema):
    success: bool
    checkout_url: Optional[str] = None
    message: str


class VendorSubscriptionDetailOut(Schema):
    id: int
    vendor_name: str
    vendor_email: str
    plan_name: Optional[str] = None
    billing_interval: Optional[str] = None
    plan_price: Optional[float] = None
    is_active: bool
    has_valid_access: bool
    subscription_expires_at: Optional[datetime] = None