from typing import List, Optional
from datetime import datetime
from ninja import Schema
from decimal import Decimal
from pydantic import Field, field_validator

# =========================================================================
# PAYMENT SETTLEMENT & BANK VALIDATION SCHEMAS
# =========================================================================

class SetTransferPinSchema(Schema):
    pin: str = Field(..., pattern=r"^\d{4}$")  # 4-digit pin
    password: str                          # account password, always required
    current_pin: Optional[str] = Field(default=None, pattern=r"^\d{4}$")  # required when a PIN already exists


class PayoutRequestInput(Schema):
    amount: Decimal = Field(..., gt=0, max_digits=10, decimal_places=2)
    settlement_account_id: int
    narration: str = Field(default="Vendor Wallet Withdrawal Payout", max_length=150)
    pin: str = Field(..., pattern=r"^\d{4}$")

class SettlementAccountViewSchema(Schema):
    id: int
    bank_name: str = ""
    account_name: str = ""
    account_number: str
    bank_code: str
    is_verified: bool

class SettlementAccountSetupPayload(Schema):
    account_number: str = Field(..., max_length=10, min_length=10)
    bank_code: str = Field(..., example="044", description="Central numeric bank code tracking string.")
    # Accepted for backward compatibility but ignored: the server resolves the real account
    # name and bank name through Paystack and never trusts the client's values.
    bank_name: Optional[str] = Field(default=None, example="Access Bank")
    account_name: Optional[str] = Field(default=None, description="Ignored: resolved by Paystack")
    # Required when a PIN or a linked account already exists (changing the payout destination).
    pin: Optional[str] = Field(default=None, pattern=r"^\d{4}$")

    @field_validator('account_number')
    def validate_nuban_numeric(cls, v: str)-> str: 
        if not v.isdigit():
            raise ValueError('Account number must contain only numerical digits.')
        return v

class PayoutLogResponseSchema(Schema):
    id: int
    amount: float
    reference: str
    status: str
    opay_order_id: Optional[str] = None  # Or transfer code mapping
    created_at: datetime
    updated_at: datetime

class VendorWalletDashboardSchema(Schema):
    id: int
    available_balance: float = Field(..., description="Money ready for immediate withdrawal.")
    escrow_balance: float = Field(..., description="Money locked until rider successfully delivers.")
    settlement_account: Optional[SettlementAccountViewSchema] = None
    recent_payouts: List[PayoutLogResponseSchema] = []
    has_transfer_pin: bool = False
    security_hold_until: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

class AccountResolveSchema(Schema):
    account_number: str = Field(..., max_length=10, min_length=10)
    bank_code: str = Field(..., description="Numeric bank code")

    