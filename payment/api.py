from ninja import Router
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone
from django.urls import reverse
from django.conf import settings
import requests
import json
import logging
import hmac
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from .models import SubscriptionPlan, VendorSubscription, SubscriptionInvoice
from accounts.models import CustomUser
from merchant.models import MerchantProfile
from accounts.utils import JWTAuth
from .schemas import (
    SubscriptionInvoiceOut, OPayCheckoutResponseOut, SubscriptionDashboardOut)


logger = logging.getLogger(__name__)

OPAY_BASE_URL = getattr(settings, "OPAY_BASE_URL", "https://sandboxapi.opaycheckout.com")

router = Router(tags=["Subscription Billing & OPay"])

@router.get("/billing", response={200: SubscriptionDashboardOut, 400: dict, 403: dict}, auth=JWTAuth())
def get_subscription_billing_dashboard(request):
    """
    Retrieves the subscription dashboard state, active tiers, and invoice history for the merchant.
    """
    user = request.auth
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated:
        return 403, {"detail": "Authentication credentials required."}

    customer_profile = getattr(user, 'merchant_profile', None) or getattr(user, 'customer_profile', None)
    if not customer_profile:
        return 400, {"detail": "Billing operation failed. Complete your vendor profile onboarding setup first."}

    subscription, _ = VendorSubscription.objects.get_or_create(vendor_profile=customer_profile)
    platform_plans = SubscriptionPlan.objects.filter(is_available=True).order_by('price')
    invoices = SubscriptionInvoice.objects.filter(vendor_profile=customer_profile).order_by('-payment_date')

    return 200, {
        "has_access": subscription.has_valid_access,
        "current_plan": subscription.plan if subscription.has_valid_access else None,
        "next_billing_date": subscription.subscription_expires_at if subscription.has_valid_access else None,
        "platform_plans": list(platform_plans),
        "invoices": list(invoices)
    }

@router.post("/billing/initiate/{plan_id}", response={200: OPayCheckoutResponseOut, 400: dict, 403: dict, 404: dict}, auth=JWTAuth())
def initiate_subscription_billing(request, plan_id: int):
    """
    Initiates an OPay payment checkout link or activates free tier subscriptions for a plan.
    """
    user = request.user
    if not user and hasattr(request, 'user'):
        user = request.user

    if not user or not user.is_authenticated:
        return 403, {"detail": "Authentication credentials required."}

    customer_profile = getattr(user, 'merchant_profile', None) or getattr(user, 'customer_profile', None)
    if not customer_profile:
        return 400, {"detail": "Billing operation failed. Complete your vendor profile onboarding setup first."}

    try:
        plan = SubscriptionPlan.objects.get(pk=plan_id, is_available=True)
    except SubscriptionPlan.DoesNotExist:
        return 404, {"detail": "Subscription plan not found."}

    # Handle Free Tier Activation instantly
    if plan.price <= 0:
        vendor_sub, _ = VendorSubscription.objects.get_or_create(vendor_profile=customer_profile)
        vendor_sub.plan = plan
        vendor_sub.is_active = True
        vendor_sub.subscription_expires_at = timezone.now() + timezone.timedelta(days=30)
        vendor_sub.save()
        return 200, {
            "success": True,
            "checkout_url": None,
            "message": f"Free tier '{plan.name}' activated successfully."
        }

    invoice = None
    try:
        with transaction.atomic():
            invoice = SubscriptionInvoice.objects.create(
                vendor_profile=customer_profile,
                plan=plan,
                amount=plan.price,
                reference_id=f"SUB-TMP-{timezone.now().strftime('%Y%m%d%H%M%S')}-{user.id}",
                status=SubscriptionInvoice.PaymentStatus.PENDING
            )
            payment_reference = f"SUB-{invoice.id}-{user.id}"
            invoice.reference_id = payment_reference
            invoice.save()

        total_amount_kobo = int((Decimal(plan.price) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        url = f"{OPAY_BASE_URL}/api/v1/international/cashier/create"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.OPAY_PUBLIC_KEY}",
            "MerchantId": str(settings.OPAY_MERCHANT_ID)
        }

        host_domain = str(settings.BACKEND_URL).rstrip('/')
        
        frontend_domain = settings.FRONTEND_URL
        
        verify_path = f"/merchant/billing/verify/{invoice.id}"
        return_url = f"{frontend_domain}{verify_path}"
        cancel_url = f"{frontend_domain}/merchant/subscriptions"
        callback_url = f"{host_domain}/api/v1/payment/billing/verify/{invoice.id}"
        
        product_name = f"{plan.name} License"
        product_desc = "Platform subscription charge for merchant account validation loops."
        user_name = f"{user.first_name} {user.last_name}".strip() or "Platform Merchant"

        payload = {
            "country": "NG",
            "reference": str(payment_reference),
            "amount": {
                "total": total_amount_kobo,
                "currency": "NGN"
            },
            "returnUrl": str(return_url),
            "callbackUrl": str(callback_url),
            "cancelUrl": str(cancel_url),
            "expireAt": 20,
            "userInfo": {
                "userEmail": str(user.email),
                "userId": str(user.id),
                "userName": str(user_name)
            },
            "product": {
                "name": str(product_name),
                "description": str(product_desc[:120])
            }
        }

        response = requests.post(url, data=json.dumps(payload), headers=headers, timeout=15)
        response_data = response.json()

        if response_data.get("code") == "00000":
            checkout_url = response_data["data"]["cashierUrl"]
            return 200, {
                "success": True,
                "checkout_url": checkout_url,
                "message": "OPay checkout session initialized successfully."
            }
        else:
            if invoice and invoice.id:
                invoice.delete()
            logger.error("OPay checkout creation rejected: %s", response_data)
            return 400, {"detail": "The payment gateway rejected the request. Please try again later."}

    except Exception:
        if invoice and invoice.id:
            invoice.delete()
        logger.exception("OPay checkout creation failed")
        return 400, {"detail": "Could not reach the payment gateway. Please try again."}

@router.post("/billing/verify/{invoice_id}", response={200: dict, 400: dict, 404: dict})
def verify_subscription_payment(request, invoice_id: int):
    """
    Secure callback and verification endpoint for OPay subscription payments.
    """
    try:
        invoice = SubscriptionInvoice.objects.get(pk=invoice_id)
    except SubscriptionInvoice.DoesNotExist:
        return 404, {"detail": "Invoice record not found."}
    
    if invoice.status == SubscriptionInvoice.PaymentStatus.SUCCESSFUL:
        return 200, {"success": True, "message": "This subscription transaction has already been verified and applied."}
        
    url = f"{OPAY_BASE_URL}/api/v1/international/cashier/status"
    payload_dict = {
        "country": "NG",
        "reference": str(invoice.reference_id)
    }
    
    payload_json_string = json.dumps(payload_dict, sort_keys=True, separators=(',', ':'))
    secret_key_bytes = bytes(settings.OPAY_SECRET_KEY, 'utf-8')
    payload_bytes = bytes(payload_json_string, 'utf-8')
    generated_signature = hmac.new(secret_key_bytes, payload_bytes, hashlib.sha512).hexdigest()
    
    headers = {
        "Content-Type": "application/json",
        "MerchantId": str(settings.OPAY_MERCHANT_ID),
        "Authorization": f"Bearer {generated_signature}"
    }
    
    try:
        response = requests.post(url, data=payload_json_string, headers=headers, timeout=15)
        response_data = response.json()
        
        api_code = response_data.get("code")
        inner_data = response_data.get("data", {})
        payment_status = inner_data.get("status") or inner_data.get("result") or response_data.get("status")

        if api_code == "00000" and payment_status in ["SUCCESSFUL", "PAID", "SUCCESS"]:
            # The amount OPay reports must cover the invoice (we send/expect minor units, kobo).
            reported = inner_data.get("amount")
            reported_total = reported.get("total") if isinstance(reported, dict) else reported
            reported_currency = reported.get("currency") if isinstance(reported, dict) else inner_data.get("currency")
            if reported_total is not None:
                try:
                    paid_kobo = Decimal(str(reported_total))
                except Exception:
                    paid_kobo = Decimal("0")
                expected_kobo = (Decimal(invoice.amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                if paid_kobo < expected_kobo or (reported_currency and str(reported_currency).upper() != "NGN"):
                    logger.error(
                        "OPay amount mismatch for invoice %s: paid=%s %s expected=%s",
                        invoice_id, paid_kobo, reported_currency, expected_kobo,
                    )
                    return 400, {"success": False, "detail": "The amount paid does not match this invoice. Please contact support."}
            else:
                logger.warning("OPay status response for invoice %s has no amount to compare", invoice_id)

            with transaction.atomic():
                # Lock the invoice so two simultaneous callbacks apply the upgrade only once.
                locked = SubscriptionInvoice.objects.select_for_update().select_related('plan', 'vendor_profile').get(pk=invoice_id)
                if locked.status != SubscriptionInvoice.PaymentStatus.SUCCESSFUL:
                    locked.status = SubscriptionInvoice.PaymentStatus.SUCCESSFUL
                    locked.save(update_fields=['status'])

                    vendor_sub, _ = VendorSubscription.objects.get_or_create(vendor_profile=locked.vendor_profile)
                    vendor_sub.plan = locked.plan
                    vendor_sub.is_active = True
                    vendor_sub.subscription_expires_at = timezone.now() + timezone.timedelta(days=30)
                    vendor_sub.save()

            return 200, {"success": True, "message": f"Payment verified! Your account has been upgraded to {invoice.plan.name}."}

        if payment_status in ["FAILED", "CLOSE", "CLOSED", "CANCELLED", "EXPIRED"]:
            invoice.status = SubscriptionInvoice.PaymentStatus.FAILED
            invoice.save(update_fields=['status'])
            return 400, {"success": False, "detail": "Payment failed or was cancelled."}

        # Still pending / unknown: leave the invoice PENDING so the real payment can still be applied.
        # (It used to be marked FAILED just because someone opened this URL too early.)
        return 400, {"success": False, "detail": "Payment not confirmed yet."}

    except Exception:
        logger.exception("OPay verification failed for invoice %s", invoice_id)
        return 400, {"detail": "Could not verify the payment right now. Please try again shortly."}
