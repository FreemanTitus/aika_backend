import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx
import jwt
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from ninja.errors import HttpError
from ninja.security import HttpBearer

from .models import CustomUser

User = get_user_model()

logger = logging.getLogger(__name__)

_ID_ALPHABET = string.ascii_uppercase + string.digits


# =========================================================================
# EXTERNAL VERIFICATION HELPERS (CAPTCHA / KYC)
# =========================================================================
async def verify_turnstile_async(token, remote_ip):
    secret = getattr(settings, "TURNSTILE_SECRET_KEY", "")
    if not secret:
        # Not configured: callers decide whether that is acceptable. Fail closed here.
        logger.error("TURNSTILE_SECRET_KEY is not configured; rejecting captcha token.")
        return False

    payload = {"secret": secret, "response": token, "remoteip": remote_ip}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=payload,
                timeout=5.0,
            )
            return bool(response.json().get("success", False))
        except (httpx.RequestError, ValueError):
            return False


def verify_turnstile_sync(token, remote_ip) -> bool:
    """Synchronous twin used by the (sync) security middleware."""
    secret = getattr(settings, "TURNSTILE_SECRET_KEY", "")
    if not secret:
        logger.error("TURNSTILE_SECRET_KEY is not configured; rejecting captcha token.")
        return False
    try:
        response = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token, "remoteip": remote_ip},
            timeout=5.0,
        )
        return bool(response.json().get("success", False))
    except (httpx.RequestError, ValueError):
        return False


def _kyc_headers():
    key = getattr(settings, "KYC_API_SECRET_KEY", "")
    if not key:
        raise HttpError(503, "Identity verification service is not configured.")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def verify_identity_via_api(id_type: str, id_number: str, first_name: str, last_name: str):
    """
    Calls a third-party KYC provider API to cross-check the submitted ID
    against official government databases.
    """
    api_url = "https://api.verified.co/v1/identities/verify"
    headers = _kyc_headers()
    payload = {"id_type": id_type, "id_number": id_number}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(api_url, json=payload, headers=headers, timeout=10.0)
            if response.status_code != 200:
                raise HttpError(400, "Failed to connect to identity verification service.")

            data = response.json()
            gov_first_name = (data.get("first_name") or "").strip().lower()
            gov_last_name = (data.get("last_name") or "").strip().lower()

            if gov_first_name != first_name.strip().lower() or gov_last_name != last_name.strip().lower():
                return False, "The name on the ID does not match your registration name."

            return True, "Verification successful."
        except httpx.RequestError:
            raise HttpError(503, "Identity verification service is currently unavailable.")


async def query_kyc_provider(id_type: str, id_number: str):
    """
    Sends an asynchronous request to the identity verification API gateway
    to fetch official government record details for a given ID number.
    """
    api_url = "https://api.verified.co/v1/identities/verify"
    key = getattr(settings, "KYC_API_SECRET_KEY", "")
    if not key:
        return False, "Identity verification service is not configured."
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"id_type": id_type, "id_number": id_number}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(api_url, json=payload, headers=headers, timeout=10.0)
            if response.status_code != 200:
                return False, "Could not verify ID number. Please check the digits and try again."

            data = response.json()
            first_name = data.get("first_name") or (data.get("data") or {}).get("first_name")
            last_name = data.get("last_name") or (data.get("data") or {}).get("last_name")

            if not first_name or not last_name:
                return False, "No valid government record found for this ID number."

            return True, {"first_name": first_name, "last_name": last_name}
        except (httpx.RequestError, ValueError):
            return False, "Identity verification network service is currently unreachable."


# =========================================================================
# TRANSACTIONAL EMAIL HELPERS
# These are plain synchronous functions. They are called from sync Ninja
# views (which run in a worker thread). Wrapping them in sync_to_async and
# calling them without `await` silently produced un-awaited coroutines, so no
# email was ever sent. Failures are logged and never bubble up to the caller:
# a mail outage must not turn a committed registration into a 500 or reveal
# (through an error) whether an email address exists.
# =========================================================================
def _send_html_email(subject: str, template: str, context: dict, text_content: str,
                     recipient_email: str, fail_silently: bool = False) -> bool:
    try:
        html_content = render_to_string(template, context)
        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [recipient_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=fail_silently)
        return True
    except Exception:
        logger.exception("Failed to send '%s' email to %s", subject, recipient_email)
        return False


def send_rider_welcome_email(recipient_email, rider_name) -> bool:
    return _send_html_email(
        "Rider Partner Application Received - Aika",
        "emails/rider_welcome.html",
        {"rider_name": rider_name},
        f"Welcome aboard, {rider_name}! Your rider partner application has been received. "
        "Our team will verify your credentials shortly.",
        recipient_email,
    )


def send_individual_welcome_email(recipient_email, user_name) -> bool:
    return _send_html_email(
        "Welcome to Aika!",
        "emails/individual_welcome.html",
        {"user_name": user_name},
        f"Welcome to Aika, {user_name}! Your individual account has been successfully created.",
        recipient_email,
    )


def send_business_welcome_email(recipient_email, business_name) -> bool:
    return _send_html_email(
        "Merchant Registration Submitted - Aika",
        "emails/business_welcome.html",
        {"business_name": business_name},
        f"Hello {business_name}, your merchant registration has been submitted and is pending admin approval.",
        recipient_email,
    )


def send_password_reset_email(recipient_email, reset_token) -> bool:
    frontend = str(getattr(settings, "FRONTEND_URL", "")).rstrip("/")
    reset_url = f"{frontend}/auth/reset-password?token={reset_token}"
    return _send_html_email(
        "Password Reset Request - Aika",
        "emails/password_reset.html",
        {"reset_url": reset_url},
        f"Reset your Aika password by visiting this link: {reset_url}",
        recipient_email,
    )


# =========================================================================
# IDENTIFIERS & JWT
# =========================================================================
def generate_alphanumeric_id(length: int = 10) -> str:
    """Cryptographically secure identifier fragment (uses `secrets`, not `random`)."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def create_jwt_for_user(user: CustomUser, expires_in_days: int = 7) -> str:
    """
    Generates a signed JWT. `token_version` lets password changes/resets and role
    changes revoke every previously issued token instantly.
    """
    now = datetime.now(timezone.utc)
    expiration_time = now + timedelta(days=expires_in_days)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": str(user.role),
        "token_version": user.token_version,
        "iat": int(now.timestamp()),
        "exp": int(expiration_time.timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def verify_jwt_token(token: str):
    """
    Verifies signature, requires `exp` and `sub`, checks active status and the
    token_version claim. Returns the CustomUser or None. Synchronous: call it
    through database_sync_to_async / sync_to_async from async code.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"require": ["exp", "sub"], "verify_exp": True},
        )
    except jwt.InvalidTokenError:  # includes ExpiredSignatureError / DecodeError
        return None

    user_id = payload.get("sub")
    token_version = payload.get("token_version", 1)
    if not user_id:
        return None

    try:
        user = CustomUser.objects.get(id=user_id)
    except (CustomUser.DoesNotExist, ValueError, TypeError):
        return None

    if not user.is_active:
        return None
    if token_version != user.token_version:
        return None
    return user


class JWTAuth(HttpBearer):
    def authenticate(self, request, token: str):
        """
        Verifies the bearer token and exposes the user as request.user so
        views can rely on request.user.role etc. Returning None => HTTP 401.
        """
        user = verify_jwt_token(token)
        if user and user.is_active:
            request.user = user
            return user
        return None


async def async_make_password(password: str) -> str:
    return await sync_to_async(make_password)(password)


# =========================================================================
# DELIVERY TARIFF
# =========================================================================
async def calculate_dynamic_delivery_fee(
    origin_lat: float, origin_lng: float,
    dest_lat: float, dest_lng: float,
) -> dict:
    """Queries OSRM for the real driving distance and returns the tariff breakdown."""
    coordinates_string = f"{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
    url = f"https://router.project-osrm.org/route/v1/driving/{coordinates_string}"
    params = {"overview": "false", "steps": "false"}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, timeout=6.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == "Ok" and len(data.get("routes", [])) > 0:
                    distance_km = data["routes"][0]["distance"] / 1000.0
                    return compute_tariff_breakdown(distance_km)
            logger.warning("OSRM Routing Node responded with status: %s", response.status_code)
        except Exception as e:
            logger.error("OpenSource Logistics Machine Handshake Fault: %s", e)

    # Safe fallback (approx. 4.5km) if the routing service is unreachable.
    # (Previously called with a non-existent keyword argument -> TypeError.)
    return compute_tariff_breakdown(4.5)


def compute_tariff_breakdown(distance_km: float = 0.0) -> dict:
    """Returns the unified flat-rate delivery tariff for the operational zone."""
    fixed_delivery_fee = Decimal("1500.00")

    return {
        "distance_km": round(distance_km, 2),
        "delivery_fee": float(fixed_delivery_fee),
        "base_fare": float(fixed_delivery_fee),
        "per_km_charge": 0.0,
    }
