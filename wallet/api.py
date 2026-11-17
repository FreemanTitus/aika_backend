import logging
from decimal import Decimal
from typing import Dict

import httpx
from asgiref.sync import sync_to_async
from ninja import Router
from ninja.errors import HttpError

from accounts.utils import JWTAuth
from merchant.models import MerchantProfile
from .models import VendorWallet, PaymentSettlementAccount
from .schemas import (
    VendorWalletDashboardSchema, SettlementAccountSetupPayload, PayoutRequestInput,
    AccountResolveSchema, SetTransferPinSchema,
)
from .services import (
    PAYSTACK, PayoutBlocked, paystack_headers, verify_with_lockout, set_pin,
    save_settlement, reserve_payout, finalize_payout,
)
from .utils import get_paystack_bank_list

logger = logging.getLogger(__name__)
router = Router(tags=["Merchant Wallet"], auth=JWTAuth())

MIN_PAYOUT = Decimal("100.00")
WEAK_PINS = {"1234", "4321", "0123", "1212", "2580", "1122"}


def _require_business(user):
    if getattr(user, "role", None) != "business":
        raise HttpError(403, "Merchant accounts only.")


async def _get_profile(user):
    try:
        return await MerchantProfile.objects.aget(user=user)
    except MerchantProfile.DoesNotExist:
        raise HttpError(404, "Complete your vendor profile setup first.")


def _raise_for_lockout(code, what="PIN"):
    if code == "locked":
        raise HttpError(429, "Too many incorrect attempts. Try again in 30 minutes.")
    if code == "invalid":
        raise HttpError(403, f"Incorrect {what}.")


async def _paystack(client, method, path, **kwargs):
    r = await client.request(method, f"{PAYSTACK}{path}", headers=paystack_headers(),
                             timeout=15.0, **kwargs)
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


@router.get("/dashboard", response=VendorWalletDashboardSchema)
async def get_merchant_wallet_details(request):
    _require_business(request.user)
    try:
        wallet = await VendorWallet.objects.select_related("profile").aget(profile__user=request.user)
    except VendorWallet.DoesNotExist:
        raise HttpError(404, "Wallet not found.")

    settlement = await PaymentSettlementAccount.objects.filter(profile=wallet.profile).afirst()
    payouts = [p async for p in wallet.payouts.order_by("-created_at")[:10]]  # no wasteful prefetch

    return {
        "id": wallet.id,
        "available_balance": float(wallet.available_balance),
        "escrow_balance": float(wallet.escrow_balance),
        "settlement_account": settlement,
        "recent_payouts": payouts,
        "has_transfer_pin": bool(wallet.transfer_pin),
        "security_hold_until": wallet.security_hold_until,
        "created_at": wallet.created_at,
        "updated_at": wallet.updated_at,
    }


@router.get("/supported-banks", response=Dict[str, str])
async def get_supported_banks(request):
    return await sync_to_async(get_paystack_bank_list)()


@router.post("/resolve-account")
async def resolve_bank_account(request, data: AccountResolveSchema):
    _require_business(request.user)
    try:
        async with httpx.AsyncClient() as client:
            code, body = await _paystack(client, "GET", "/bank/resolve", params={
                "account_number": data.account_number, "bank_code": data.bank_code})
    except httpx.RequestError:
        raise HttpError(504, "Account resolution service timeout.")
    if code == 200 and body.get("status"):
        return {"status": "success", "account_name": body["data"].get("account_name")}
    raise HttpError(400, "Could not resolve account details.")


@router.post("/bank-setup")
async def setup_merchant_bank_account(request, data: SettlementAccountSetupPayload):
    _require_business(request.user)
    profile = await _get_profile(request.user)

    # Changing or adding a bank account when a PIN or account already exists needs the PIN.
    wallet = await VendorWallet.objects.filter(profile=profile).afirst()
    has_account = await PaymentSettlementAccount.objects.filter(profile=profile).aexists()
    if has_account or (wallet and wallet.transfer_pin):
        if not data.pin:
            raise HttpError(403, "Transfer PIN required to change your bank account.")
        code = await sync_to_async(verify_with_lockout)(profile, lambda w: w.verify_pin(data.pin))
        _raise_for_lockout(code)

    bank_code = "".join(c for c in data.bank_code if c.isdigit())
    banks = await sync_to_async(get_paystack_bank_list)()
    code_to_name = {v: k for k, v in banks.items()}
    if bank_code not in code_to_name:
        raise HttpError(400, "Unsupported bank.")

    try:
        async with httpx.AsyncClient() as client:
            # 1. Paystack tells us who owns the account; the client's account_name is never trusted.
            code, body = await _paystack(client, "GET", "/bank/resolve", params={
                "account_number": data.account_number, "bank_code": bank_code})
            if code != 200 or not body.get("status"):
                raise HttpError(400, "Could not verify this bank account.")
            account_name = body["data"]["account_name"]

            # 2. Create the transfer recipient once, here, instead of on every payout.
            code, body = await _paystack(client, "POST", "/transferrecipient", json={
                "type": "nuban", "name": account_name, "account_number": data.account_number,
                "bank_code": bank_code, "currency": "NGN"})
            if code not in (200, 201) or not body.get("status"):
                raise HttpError(400, "Could not register this bank account for payouts.")
            recipient_code = body["data"]["recipient_code"]
    except httpx.RequestError:
        raise HttpError(504, "Bank verification service timeout. Please try again.")

    await sync_to_async(save_settlement)(
        profile, bank_name=code_to_name[bank_code], bank_code=bank_code,
        account_number=data.account_number, account_name=account_name,
        recipient_code=recipient_code)

    return {"status": "success",
            "message": f"Settlement account linked for '{account_name}'.",
            "account_name": account_name}


@router.post("/vendor/set-pin")
async def set_vendor_transfer_pin(request, payload: SetTransferPinSchema):
    _require_business(request.user)
    profile = await _get_profile(request.user)
    pin = payload.pin.strip()

    if len(set(pin)) == 1 or pin in WEAK_PINS:
        raise HttpError(400, "Choose a less predictable PIN.")

    user = request.user
    # Account password is always required...
    code = await sync_to_async(verify_with_lockout)(
        profile, lambda w: user.check_password(payload.password))
    _raise_for_lockout(code, "password")

    # ...and so is the current PIN when one exists.
    wallet = await VendorWallet.objects.filter(profile=profile).afirst()
    if wallet and wallet.transfer_pin:
        if not payload.current_pin:
            raise HttpError(403, "Current PIN required.")
        code = await sync_to_async(verify_with_lockout)(
            profile, lambda w: w.verify_pin(payload.current_pin))
        _raise_for_lockout(code)

    await sync_to_async(set_pin)(profile, pin)
    return {"status": "success",
            "message": "Transfer PIN updated. If you changed an existing PIN, payouts are paused for 24 hours."}


@router.post("/vendor/payout/request")
async def request_vendor_payout(request, data: PayoutRequestInput):
    _require_business(request.user)
    profile = await _get_profile(request.user)

    amount = data.amount.quantize(Decimal("0.01"))
    if amount < MIN_PAYOUT:
        raise HttpError(400, f"Minimum payout is ₦{MIN_PAYOUT:,.2f}.")

    settlement = await PaymentSettlementAccount.objects.filter(
        id=data.settlement_account_id, profile=profile, is_verified=True,
        recipient_code__isnull=False).afirst()
    if not settlement:
        raise HttpError(404, "Verified settlement account not found. Please set up your bank account.")

    wallet = await VendorWallet.objects.filter(profile=profile).afirst()
    if not wallet or not wallet.transfer_pin:
        raise HttpError(400, "Set up your 4-digit transfer PIN first.")

    # PIN check with lockout (failures persist)
    code = await sync_to_async(verify_with_lockout)(profile, lambda w: w.verify_pin(data.pin))
    _raise_for_lockout(code)

    # Reserve funds under a row lock BEFORE touching Paystack
    try:
        payout = await sync_to_async(reserve_payout)(profile, amount)
    except PayoutBlocked as e:
        if e.code == "hold":
            raise HttpError(403, "Payouts are paused for 24 hours after a PIN or bank account change.")
        raise HttpError(400, "Insufficient available wallet balance.")

    pending_response = {
        "status": "processing",
        "message": "Your payout is being processed. The final status will appear in your payout history.",
        "reference": payout.reference,
    }

    try:
        async with httpx.AsyncClient() as client:
            code, body = await _paystack(client, "POST", "/transfer", json={
                "source": "balance",
                "amount": int(amount * 100),
                "recipient": settlement.recipient_code,
                "reference": payout.reference,       # Paystack dedupes on this, so retries are safe
                "reason": data.narration,
            })
    except httpx.RequestError:
        # Ambiguous: Paystack may have received it. Do NOT refund. Webhooks/reconcile will settle it.
        logger.exception("Transfer request outcome unknown for %s", payout.reference)
        return pending_response

    transfer = body.get("data") or {}
    transfer_code = transfer.get("transfer_code")

    if code in (200, 201) and body.get("status"):
        tstatus = transfer.get("status")
        if tstatus == "success":
            await sync_to_async(finalize_payout)(payout.reference, "success", transfer_code)
            return {"status": "success", "message": "Payout completed.", "reference": payout.reference}
        if tstatus in ("failed", "reversed"):
            await sync_to_async(finalize_payout)(payout.reference, "failed", transfer_code, reason=tstatus)
            raise HttpError(400, "The bank transfer failed. Your balance was restored.")
        if tstatus == "otp":
            logger.error("Paystack transfer OTP is enabled; disable it for API transfers (%s)", payout.reference)
            await sync_to_async(finalize_payout)(payout.reference, "failed", transfer_code, reason="otp_required")
            raise HttpError(400, "Payouts are temporarily unavailable. Please contact support.")
        # 'pending' etc.: keep PROCESSING until the transfer.* webhook arrives
        await sync_to_async(
            lambda: type(payout).objects.filter(pk=payout.pk).update(opay_order_id=transfer_code))()
        return pending_response

    if 400 <= code < 500:
        # Definitive rejection: no money moved. Safe to restore the balance.
        logger.error("Paystack rejected transfer %s: %s", payout.reference, body.get("message"))
        await sync_to_async(finalize_payout)(
            payout.reference, "failed", reason=str(body.get("message", "rejected")))
        raise HttpError(400, "The bank transfer could not be started. Your balance was restored.")

    # 5xx or anything unexpected: ambiguous, leave PROCESSING
    logger.error("Unexpected Paystack response %s for %s", code, payout.reference)
    return pending_response