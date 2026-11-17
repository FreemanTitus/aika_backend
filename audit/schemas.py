from ninja import Schema
from typing import List, Optional
from datetime import datetime


class AuditLogOut(Schema):
    id: int
    user_email: Optional[str] = None
    action: str
    target_entity: str
    details: Optional[str] = None
    created_at: datetime

class DisputeOut(Schema):
    id: int
    order_id: str
    customer_name: str
    reason: str
    amount: float
    status: str
    raised_by: str
    created_at: datetime

class CustomerDisputeCreateSchema(Schema):
    order_id: str
    reason: str
    amount: float
    bank_name: str
    account_number: str
    account_name: str

class CustomerDisputeOut(Schema):
    id: int
    order_id: str
    reason: str
    amount: float
    status: str
    created_at: datetime