from ninja import Schema
from typing import List, Optional
from datetime import datetime

class BroadcastCreateSchema(Schema):
    title: str
    message: str
    target_audience: str

class PromoCodeCreateSchema(Schema):
    code: str
    discount_type: str
    value: float
    min_order_amount: float
    usage_limit: int
    expires_at: datetime