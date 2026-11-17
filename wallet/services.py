import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote

import httpx
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone

from .models import VendorWallet, PaymentSettlementAccount, PayoutLog

logger = logging.getLogger(__name__)

PAYSTACK = "https://api.paystack.co"
MAX_ATTEMPTS = 5
LOCK_MINUTES = 30
SECURITY_HOLD_HOURS = 24


class PayoutBlocked(Exception):
    def __init__(self, code):
        self.code = code


def paystack_headers():
    return {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"}


def verify_with_lockout(profile, check):
    """
    Runs check(wallet) -> bool under a row lock and persists failure counters.
    Returns 'ok' | 'invalid' | 'locked'. Never raises, so the counter commits.
    """
    with transaction.atomic():
        wallet, _ = VendorWallet.objects.select_for_update().get_or_create(profile=profile)
        now = timezone.now()
        if wallet.pin_locked_until and wallet.pin_locked_until > now:
            return "locked"
        if check(wallet):
            if wallet.pin_failed_attempts or wallet.pin_locked_until:
                wallet.pin_failed_attempts = 0
                wallet.pin_locked_until = None
                wallet.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
            return "ok"
        wallet.pin_failed_attempts += 1
        if wallet.pin_failed_attempts >= MAX_ATTEMPTS:
            wallet.pin_locked_until = now + timedelta(minutes=LOCK_MINUTES)
            wallet.pin_failed_attempts = 0
        wallet.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
        return "invalid"


def set_pin(profile, new_pin):
    with transaction.atomic():
        wallet, _ = VendorWallet.objects.select_for_update().get_or_create(profile=profile)
        had_pin = bool(wallet.transfer_pin)
        wallet.transfer_pin = make_password(new_pin)
        wallet.pin_failed_attempts = 0
        wallet.pin_locked_until = None
        fields = ["transfer_pin", "pin_failed_attempts", "pin_locked_until"]
        if had_pin:  # a changed PIN pauses payouts
            wallet.security_hold_until = timezone.now() + timedelta(hours=SECURITY_HOLD_HOURS)
            fields.append("security_hold_until")
        wallet.save(update_fields=fields)


def save_settlement(profile, *, bank_name, bank_code, account_number, account_name, recipient_code):
    with transaction.atomic():
        wallet, _ = VendorWallet.objects.select_for_update().get_or_create(profile=profile)
        existing = PaymentSettlementAccount.objects.filter(profile=profile).first()
        changed = bool(existing) and (
            existing.account_number != account_number or existing.bank_code != bank_code
        )
        PaymentSettlementAccount.objects.update_or_create(
            profile=profile,
            defaults=dict(bank_name=bank_name, bank_code=bank_code, account_number=account_number,
                          account_name=account_name, recipient_code=recipient_code, is_verified=True),
        )
        if changed:
            wallet.security_hold_until = timezone.now() + timedelta(hours=SECURITY_HOLD_HOURS)
            wallet.save(update_fields=["security_hold_until"])


def reserve_payout(profile, amount: Decimal) -> PayoutLog:
    """Atomically check the balance, deduct it and log the payout as PROCESSING."""
    with transaction.atomic():
        wallet = VendorWallet.objects.select_for_update().get(profile=profile)
        if wallet.security_hold_until and wallet.security_hold_until > timezone.now():
            raise PayoutBlocked("hold")
        if wallet.available_balance < amount:
            raise PayoutBlocked("insufficient")
        wallet.available_balance -= amount
        wallet.save(update_fields=["available_balance", "updated_at"])
        # Paystack transfer references must be lowercase, 16-50 chars: [a-z0-9_-]
        return PayoutLog.objects.create(
            wallet=wallet, amount=amount,
            reference=f"pk-po-{uuid.uuid4().hex[:20]}",
            status=PayoutLog.PayoutStatus.PROCESSING,
        )


def finalize_payout(reference, outcome, transfer_code=None, reason=""):
    """
    Idempotent settlement. outcome: 'success' | 'failed' (failed also covers reversed).
    Safe to call repeatedly from the API response, webhooks and the reconcile job.
    """
    with transaction.atomic():
        payout = PayoutLog.objects.select_for_update().filter(reference=reference).first()
        if payout is None:
            return None
        wallet = VendorWallet.objects.select_for_update().get(pk=payout.wallet_id)

        if outcome == "success":
            if payout.balance_restored:
                # Paystack paid out money we had already returned: re-deduct and alert.
                wallet.available_balance -= payout.amount
                wallet.save(update_fields=["available_balance", "updated_at"])
                payout.balance_restored = False
                logger.critical("Payout %s succeeded after refund; wallet re-debited", reference)
            payout.status = PayoutLog.PayoutStatus.SUCCESS
        else:
            if not payout.balance_restored:
                wallet.available_balance += payout.amount
                wallet.save(update_fields=["available_balance", "updated_at"])
                payout.balance_restored = True
            payout.status = PayoutLog.PayoutStatus.FAILED

        if transfer_code:
            payout.opay_order_id = transfer_code
        if reason:
            payout.failure_reason = reason[:255]
        payout.save()
        return payout


def reconcile_payout(reference):
    """Asks Paystack what happened to a payout stuck in PROCESSING."""
    payout = PayoutLog.objects.filter(
        reference=reference, status=PayoutLog.PayoutStatus.PROCESSING).first()
    if not payout:
        return
    try:
        r = httpx.get(f"{PAYSTACK}/transfer/verify/{quote(reference, safe='')}",
                      headers=paystack_headers(), timeout=15.0)
    except httpx.RequestError:
        return
    if r.status_code == 404:
        # Paystack never received it (the original request died before arriving).
        if payout.created_at < timezone.now() - timedelta(minutes=10):
            finalize_payout(reference, "failed", reason="Transfer never reached Paystack")
        return
    if r.status_code != 200:
        return
    data = (r.json().get("data") or {})
    status = data.get("status")
    if status == "success":
        finalize_payout(reference, "success", data.get("transfer_code"))
    elif status in ("failed", "reversed"):
        finalize_payout(reference, "failed", data.get("transfer_code"), reason=status)