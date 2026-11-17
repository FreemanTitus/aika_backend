from ninja import Schema, Field
from typing import List, Optional
from datetime import datetime


class RiderWalletSchema(Schema):
    wallet_balance: float
    total_deliveries: int
    platform_commission_withheld: float
    gross_delivery_volume: float


class RiderDashboardResponse(Schema):
    rider_name: str
    status: str
    is_vetted: bool
    rating: float
    wallet: RiderWalletSchema
    pending_tasks_count: int
    active_deliveries: List[dict]


class RiderStatusSchema(Schema):
    status: str

class ActiveOrderSchema(Schema):
    id: int
    order_reference: str
    marketplace_id: str
    sender_name: str
    pickup_address: str
    receiver_name: str
    delivery_address: str
    package_description: str
    delivery_fee: float
    status: str
    created_at: datetime

class ActiveOrdersListSchema(Schema):
    status: str
    active_orders: List[ActiveOrderSchema]

class RiderPreferencesSchema(Schema):
    payout_schedule: str  # 'daily' or 'weekly'
    payout_method: str    # 'cash' or 'account'
    bank_name: Optional[str] = None
    bank_code: Optional[str] = None
    account_number: Optional[str] = None
    account_name: Optional[str] = None

class SubmitRiderReviewSchema(Schema):
    rating: int = Field(..., ge=1, le=5, description="Star rating between 1 and 5")
    comment: Optional[str] = Field(default=None, max_length=500)


    