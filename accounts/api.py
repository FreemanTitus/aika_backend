# accounts/api.py
import json
import logging
import threading
from typing import List
from django.utils import timezone
from django.http import JsonResponse
from django.db import IntegrityError
from ninja import Router
from ninja.errors import HttpError
from django.db import transaction
from django.contrib.auth import get_user_model
# Core models and updated Pydantic schemas
from accounts.models import CustomUser, CoverageArea
from customers.models import IndividualProfile, Referral, CustomerWallet
from customers.utils import handle_individual_registration
from merchant.models import MerchantProfile
from rider.models import RiderProfile
from accounts.utils import (
    create_jwt_for_user, send_rider_welcome_email, 
    send_individual_welcome_email, send_business_welcome_email,
    send_password_reset_email, JWTAuth
)
from decimal import Decimal
from .schemas import (
    BusinessRegistrationInput, UserLoginSchema, 
    IndividualRegistrationInput, RegistrationResponse, RiderRegistrationInput, RiderLoginResponse,
    PasswordResetRequestSchema, PasswordResetConfirmSchema, MessageResponse, PasswordChangeSchema,
    PasswordChangeResponse, LocationResponse
)
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.hashers import make_password, check_password
from django.core.exceptions import ValidationError
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, smart_str


# Instantiating the Ninja router for the authentication module
router = Router(
    tags=["Authentication"]
)


User = get_user_model()
token_generator = PasswordResetTokenGenerator()
# Initialize a standard production logger
logger = logging.getLogger("wegofera.accounts")

DUMMY_PASSWORD = "DummyPassword123!"  
DUMMY_HASHED_PASSWORD = make_password(DUMMY_PASSWORD)


def authenticate_by_email(email, password):
    clean_email = email.strip().lower()
    try:
        user = CustomUser.objects.get(email__iexact=clean_email)
    except CustomUser.DoesNotExist:
        check_password(DUMMY_PASSWORD, DUMMY_HASHED_PASSWORD)
        return None
    if not user.check_password(password):
        return None
    return user

def validate_registration_password(data) -> str:
    """
    Validates the password against AUTH_PASSWORD_VALIDATORS and returns its hash.
    (Previously this returned None, so User.objects.create(password=None) failed
    and every registration crashed.)
    """
    try:
        validate_password(data.password)
    except ValidationError as e:
        raise HttpError(400, " ".join(e.messages))
    return make_password(data.password)


# =========================================================================
# 1. UNIFIED REGISTRATION ENDPOINT
# =========================================================================

def db_register_individual(data, hashed_password):
    with transaction.atomic():
        user = User.objects.create(
            email=data.email.strip().lower(),
            password=hashed_password,
            role=User.Role.INDIVIDUAL,
            first_name=data.first_name,
            last_name=data.last_name,
            phone_number=data.phone_number,
            base_location_id=data.base_location,
            street_address=data.street_address,
            is_terms_accepted=True,
            terms_accepted_at=timezone.now()
        )

        # Automatically provision a customer wallet with a zero balance
        CustomerWallet.objects.create(
            user=user,
            balance=Decimal('0.00'),
            currency='NGN',
            is_active=True
        )

        # Check and handle referral code if provided
        inviter_user = None
        referral_code_input = getattr(data, 'referral_code', None)
        if referral_code_input:
            clean_code = str(referral_code_input).strip()
            if clean_code:
                try:
                    inviter_profile = IndividualProfile.objects.get(referral_code__iexact=clean_code)
                    inviter_user = inviter_profile.user
                except IndividualProfile.DoesNotExist:
                    pass  
        profile = IndividualProfile.objects.create(
            user=user,
            first_name=data.first_name,
            last_name=data.last_name,
            referred_by=inviter_user if (inviter_user and inviter_user != user) else None
        )

        if inviter_user and inviter_user != user:
            Referral.objects.create(
                inviter=inviter_user,
                invitee=user
            )
        return user.id, profile.id


def db_register_business(data, hashed_password):
    with transaction.atomic():
        user = User.objects.create(
            email=data.email.strip().lower(),
            password=hashed_password,
            role=User.Role.BUSINESS,
            first_name=data.company_name,
            phone_number=data.phone_number,
            street_address=data.street_address or "Vendor Depot",
            base_location_id=data.base_location,
            is_terms_accepted=True,
            terms_accepted_at=timezone.now(),
        )
        profile = MerchantProfile.objects.create(
            user=user,
            company_name=data.company_name,
            business_type=data.business_category,
            ecommerce_sub_category=data.ecommerce_sub_category,
        )
        return user.id, profile.id

def db_register_rider(data, hashed_password):
    with transaction.atomic():
        user = User.objects.create(
            email=data.email.strip().lower(),
            first_name=data.first_name,
            last_name=data.last_name,
            role=User.Role.RIDER,
            password=hashed_password,
            phone_number=data.phone_number,
            base_location_id=data.base_location,
            street_address=data.street_address,
            is_terms_accepted=True,
            terms_accepted_at=timezone.now()
        )
        
        rider = RiderProfile.objects.create(
            user=user,
            first_name=data.first_name,
            last_name=data.last_name,
            bike_plate_number=data.bike_plate_number,
            device_type=data.device_type,
            employment_type=getattr(data, 'employment_type', 'full_time'),
            payout_schedule=getattr(data, 'payout_schedule', 'daily'),
            id_type=getattr(data, 'id_type', None),
            id_number=getattr(data, 'id_number', None),
            guarantor_name=getattr(data, 'guarantor_name', None),
            guarantor_phone=getattr(data, 'guarantor_phone', None),
            guarantor_address=getattr(data, 'guarantor_address', None),
            is_vetted=False,
            is_id_verified=False  
        )
        
        return user.id, rider.id

def execute_rider_registration(service, data) -> dict:
    hashed_password = validate_registration_password(data)

    try:
        user_id, profile_id = service(data, hashed_password)
        return {
            "status": "success",
            "user_id": user_id,
            "profile_id": profile_id
        }

    except IntegrityError as exc:
        error_msg = str(exc).lower()
        underlying_err = exc.__cause__
        
        if underlying_err:
            sql_state = getattr(underlying_err, "sqlstate", None) or getattr(underlying_err, "pgcode", None)
            
            if sql_state == "23505" and "email" in str(underlying_err).lower():
                raise HttpError(400, "An account with this email already exists.")
                
            if sql_state == "23503" and "base_location" in str(underlying_err).lower():
                raise HttpError(400, "The selected base location is invalid or no longer exists.")

        if "unique constraint failed" in error_msg and "email" in error_msg:
            raise HttpError(400, "An account with this email already exists.")
            
        if "foreign key constraint failed" in error_msg:
            raise HttpError(400, "The selected base location is invalid.")
        
        logger.error(
            "Integrity failure during rider registration.", 
            exc_info=True,
            extra={"input_email": getattr(data, "email", None), "base_location": getattr(data, "base_location", None)}
        )
        raise HttpError(500, "A database consistency error occurred.")

    except Exception:
        logger.exception(
            "Unexpected rider registration error",
            extra={"input_email": getattr(data, "email", None), "base_location": getattr(data, "base_location", None)}
        )
        raise HttpError(500, "An unexpected error occurred.")


def execute_registration(service, data) -> dict:
    hashed_password = validate_registration_password(data)

    try:
        user_id, profile_id = service(data, hashed_password)
        return {
            "status": "success",
            "user_id": user_id,
            "profile_id": profile_id
        }

    except IntegrityError as exc:
        error_msg = str(exc).lower()
        underlying_err = exc.__cause__
        
        if underlying_err:
            sql_state = getattr(underlying_err, "sqlstate", None) or getattr(underlying_err, "pgcode", None)
            
            if sql_state == "23505" and "email" in str(underlying_err).lower():
                raise HttpError(400, "An account with this email already exists.")
                
            if sql_state == "23503" and "base_location" in str(underlying_err).lower():
                raise HttpError(400, "The selected base location is invalid or no longer exists.")

        if "unique constraint failed" in error_msg and "email" in error_msg:
            raise HttpError(400, "An account with this email already exists.")
            
        if "foreign key constraint failed" in error_msg:
            raise HttpError(400, "The selected base location is invalid.")
        
        logger.error(
            "Unexpected integrity failure during registration flow.",
            exc_info=True,
            extra={"input_email": data.email, "base_location": data.base_location}
        )
        raise HttpError(500, "A database consistency error occurred.")

    except Exception:
        logger.exception(
            "Unexpected registration error",
            extra={
                "input_email": data.email,
                "base_location": data.base_location,
            },
        )
        raise HttpError(500, "An unexpected error occurred.")


@router.post("/register/individual", response={201: RegistrationResponse})
def register_individual_api(request, data: IndividualRegistrationInput):
    response_data = execute_registration(db_register_individual, data)
    response_data["message"] = "Welcome to Aika!"
    send_individual_welcome_email(data.email, data.first_name)
    return 201, response_data

@router.post("/register/rider", response={201: RegistrationResponse})
def register_rider_api(request, data: RiderRegistrationInput):
    response_data = execute_rider_registration(db_register_rider, data)
    response_data['message'] = "Welcome to Aika"
    send_rider_welcome_email(data.email, data.first_name)
    return 201, response_data

@router.post("/register/business", response={201: RegistrationResponse})
def register_business_api(request, data: BusinessRegistrationInput):
    response_data = execute_registration(db_register_business, data)
    response_data["message"] = "Merchant registration submitted for approval!"
    send_business_welcome_email(data.email, data.company_name)
    return 201, response_data

# =========================================================================
# 2. DUAL-PROFILE USER LOGIN ENDPOINT
# =========================================================================
@router.post("/login")
def login_user_api(request, data: UserLoginSchema):
    """
    Authenticates system users synchronously, dynamically resolving the 
    split profile architecture (Individual vs Merchant) based on structural roles.
    """
    user = authenticate_by_email(data.email, data.password)
    if user is None:
        return JsonResponse({"detail": "Invalid email or password combination."}, status=401)

    if not user.is_active:
        return JsonResponse(
            {"error": "This control deck profile has been deactivated by core administration."}, 
            status=403
        )

    profile_id = None
    is_approved = True 
    business_type = ''

    if user.role == CustomUser.Role.BUSINESS:
        try:
            merchant = MerchantProfile.objects.get(user=user)
            profile_id = merchant.id
            is_approved = merchant.is_approved  
            business_type = merchant.business_type
        except MerchantProfile.DoesNotExist:
            is_approved = False
    elif user.role == CustomUser.Role.INDIVIDUAL:
        try:
            customer = IndividualProfile.objects.get(user=user)
            profile_id = customer.id
            is_approved = True  
        except IndividualProfile.DoesNotExist:
            pass

    # Dashboard redirection assignments
    redirect_target = "/customer/dashboard"
    if user.is_superuser:
        redirect_target = "/admin/dashboard"
    elif user.role == CustomUser.Role.STAFF:
        redirect_target = "/staff/dashboard"
    elif user.role == CustomUser.Role.AGENT:
        redirect_target = "/agent/dashboard"
    elif user.role == CustomUser.Role.BUSINESS:
        redirect_target = "/merchant/dashboard"

    token = create_jwt_for_user(user)

    return {
        "status": "success",
        "access_token": token,
        "id": user.id,
        "profile_id": profile_id,
        "email": user.email,
        "role": user.role,
        "business_type": business_type,
        "base_location": user.base_location_id,
        "is_terms_accepted": user.is_terms_accepted,
        "is_approved": is_approved,
        "routing_target": redirect_target
    }


@router.post("/login/rider", response=RiderLoginResponse)
def login_rider_api(request, data: UserLoginSchema):
    user = authenticate_by_email(data.email, data.password)
    if user is None:
        raise HttpError(401, "Invalid email or password.")

    if not user.is_active:
        raise HttpError(403, "Account deactivated by administration.")

    if user.role != CustomUser.Role.RIDER:
        raise HttpError(403, "This portal is for riders only.")

    try:
        rider = RiderProfile.objects.get(user=user)
    except RiderProfile.DoesNotExist:
        raise HttpError(401, "Rider profile not initialized.")

    if not rider.is_vetted:
        raise HttpError(403, "Account pending administrative vetting.")

    token = create_jwt_for_user(user)
    
    return {
        "status": "success",
        "id": user.id,
        "access_token": token,
        "profile_id": rider.id,
        "role": user.role,
        "routing_target": "/rider/dashboard"
    }

# ################################################
#  PASSWORD RESET ENDPOINTS
# ################################################
@router.post("/password/reset/request")
def request_password_reset(request, data: PasswordResetRequestSchema):
    """
    Step 1: User submits email. If user exists, generate token and dispatch email.
    """
    clean_email = data.email.strip().lower()
    try:
        user = User.objects.get(email__iexact=clean_email)
    except User.DoesNotExist:
        return {"message": "If an account with that email exists, a password reset link has been sent."}

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = token_generator.make_token(user)
    combined_token = f"{uid}:{token}"

    # Send in the background so the response time is the same whether or not the
    # address exists (prevents account enumeration through timing or SMTP errors).
    threading.Thread(
        target=send_password_reset_email,
        args=(user.email, combined_token),
        daemon=True,
    ).start()

    return {"message": "If an account with that email exists, a password reset link has been sent."}

@router.post("/password/reset/confirm", response={200: MessageResponse, 400: MessageResponse})
def confirm_password_reset(request, data: PasswordResetConfirmSchema):
    """
    Step 2: User submits the token from their email along with their new password.
    """
    try:
        uid_b64, token = data.token.split(":")
        uid = smart_str(urlsafe_base64_decode(uid_b64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return 400, {"message": "Invalid or expired password reset link."}

    if not token_generator.check_token(user, token):
        return 400, {"message": "Invalid or expired password reset token."}

    if not user.is_active:
        return 400, {"message": "Invalid or expired password reset link."}

    try:
        validate_password(data.new_password, user=user)
    except ValidationError as e:
        return 400, {"message": " ".join(e.messages)}

    user.set_password(data.new_password)
    user.token_version += 1  # Invalidate existing JWT tokens instantly
    user.save()

    return {"message": "Password successfully reset. You can now log in with your new credentials."}

@router.post("/password/change", auth=JWTAuth(), response={200: PasswordChangeResponse, 400: MessageResponse})
def change_password(request, data: PasswordChangeSchema):
    """
    Allows an authenticated user to change their password by providing their current and new password.
    """
    user = request.auth

    if not user.check_password(data.current_password):
        return 400, {"message": "Current password is incorrect."}

    try:
        validate_password(data.new_password, user=user)
    except ValidationError as e:
        return 400, {"message": " ".join(e.messages)}

    user.set_password(data.new_password)
    user.token_version += 1  # Invalidate existing JWT tokens instantly
    user.save()

    return 200, {
        "message": "Password successfully updated.",
        "access_token": create_jwt_for_user(user),  # keeps THIS session signed in
    }

# Mobile App
@router.post("/customer/login")
def customer_login_api(request, data: UserLoginSchema):
    """
    Dedicated authentication endpoint for customer accounts 
    tailored for mobile (Flutter) and web storefront users.
    """
    user = authenticate_by_email(data.email, data.password)
    if user is None:
        raise HttpError(401, "Invalid email or password combination.")

    if not user.is_active:
        raise HttpError(403, "This customer account has been deactivated.")

    if user.role != CustomUser.Role.INDIVIDUAL:
        raise HttpError(403, "This portal is for customer accounts only.")

    customer, _ = IndividualProfile.objects.get_or_create(user=user)
    profile_id = customer.id

    token = create_jwt_for_user(user)

    return {
        "status": "success",
        "access_token": token,
        "id": user.id,
        "profile_id": profile_id,
        "email": user.email,
        "role": user.role,
        "base_location": user.base_location_id,
        "is_terms_accepted": getattr(user, 'is_terms_accepted', True),
        "routing_target": "/customer/dashboard"
    }

@router.get("/locations", response=List[LocationResponse])
def get_active_locations(request):
    """
    Returns a list of all active coverage areas and their states.
    """
    areas = CoverageArea.objects.select_related('state').filter(is_active=True)
    
    return [
        {
            "id": area.id, 
            "name": area.name, 
            "state_name": area.state.name
        }
        for area in areas
    ]