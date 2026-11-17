from typing import Literal, Optional
from ninja import Schema
from pydantic import BaseModel
from pydantic import EmailStr, field_validator, model_validator


class BaseRegistrationInput(Schema):
    email: EmailStr
    password: str
    phone_number: str
    base_location: int
    street_address: str
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

class IndividualRegistrationInput(BaseRegistrationInput):
    account_role: Literal['individual']
    first_name: str
    last_name: str
    referral_code: Optional[str] = None

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def strip_names(cls, value):
        return cls._strip(value)

class BusinessRegistrationInput(BaseRegistrationInput):
    """
    Unified serialization gate validating input data matrices for both 
    Individual and Business storefront accounts at the high-concurrency Pydantic boundary.
    """
    # Global Channel Attributes
    account_role: Literal['business']

    # Conditional Business Storefront Attributes (Defaults to None)
    company_name: str
    business_category: Literal['restaurant', 'e_commerce', 'other']
    ecommerce_sub_category: Optional[Literal['gadgets', 'apparel', 'appliances', 'beauty', 'general']] = None
    
    @field_validator("company_name", mode="before")
    @classmethod
    def normalize_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value
    
    @model_validator(mode="after")
    def validate_business(self):
        if (
            self.business_category == "e_commerce"
            and not self.ecommerce_sub_category
        ):
            raise ValueError(
                "E-commerce businesses require a sub-category."
            )

        return self


class RiderRegistrationInput(BaseRegistrationInput):
    account_role: Literal['rider']
    first_name: str
    last_name: str
    bike_plate_number: str
    device_type: str
    
    # Employment and Payout Configuration
    employment_type: Literal['full_time', 'casual'] = 'full_time'
    payout_schedule: Literal['daily', 'weekly', 'monthly'] = 'daily'
    
    # KYC and Security Fields for Bike Theft Protection
    id_type: Optional[str] = None  # e.g., 'nin', 'drivers_license', 'voters_card'
    id_number: Optional[str] = None
    guarantor_name: Optional[str] = None
    guarantor_phone: Optional[str] = None
    guarantor_address: Optional[str] = None


class RegistrationResponse(Schema):
    status: str
    message: str
    user_id: int
    profile_id: str




# =========================================================================
# 3. USER LOGIN SCHEMA
# =========================================================================
class UserLoginSchema(Schema):
    """
    Validates credentials for incoming user authentication requests.
    """
    email: EmailStr
    password: str
    # Pydantic maps standard field defaults natively instead of using kw_only configurations
    remember_me: bool = False


class LoginResponseSchema(BaseModel):
    status: str
    access_token: str
    id: str
    profile_id: Optional[str] = None
    email: str
    role: str
    business_type: Optional[str] = ""
    base_location: Optional[str] = None
    is_terms_accepted: bool
    is_approved: bool
    routing_target: str

class RiderLoginResponse(Schema):
    status: str
    access_token: str
    profile_id: str
    role: str
    routing_target: str


class PasswordResetRequestSchema(Schema):
    email: EmailStr

class PasswordResetConfirmSchema(Schema):
    token: str
    new_password: str

class MessageResponse(Schema):
    message: str


class PasswordChangeResponse(Schema):
    message: str
    # A password change revokes every issued token (token_version bump); this fresh one lets
    # the client that made the change stay signed in.
    access_token: Optional[str] = None


class PasswordChangeSchema(Schema):
    current_password: str
    new_password: str


class CustomerReviewSchema(Schema):
    name: str
    role: str
    comment: str

class IdPreviewInput(BaseModel):
    id_type: str  # e.g., 'nin', 'drivers_license', 'voters_card'
    id_number: str

class LocationResponse(Schema):
    id: int
    name: str
    state_name: str