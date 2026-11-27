from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from decimal import Decimal
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist


def _get_active_vendor_subscription(merchant, now: datetime):
    try:
        sub = merchant.vendorsubscription
    except (AttributeError, ObjectDoesNotExist):
        return None

    if sub and sub.is_active and sub.subscription_expires_at and now < sub.subscription_expires_at:
        return sub
    return None


@dataclass(frozen=True)
class FulfillmentOption:
    code: str            # "platform" | "self"
    label: str
    fee: Optional[Decimal]  # Decimal for "self" (vendor's fixed fee), None for "platform" (computed by distance/zone)
    is_free: bool

@dataclass(frozen=True)
class CheckoutShippingOptions:
    vendor_id: int
    fulfillment_type: str             # "platform" | "self" | "hybrid"
    allowed_methods: List[str]        # e.g. ["platform"], ["self"], or ["platform", "self"]
    default_method: str               # "platform" | "self"
    allows_selection: bool            # True only when fulfillment_type == "hybrid"
    allow_nationwide: bool
    self_delivery_fee: Decimal
    options: List[FulfillmentOption]

    def to_dict(self) -> Dict:
        return {
            "vendor_id": self.vendor_id,
            "fulfillment_type": self.fulfillment_type,
            "allowed_methods": self.allowed_methods,
            "default_method": self.default_method,
            "allows_selection": self.allows_selection,
            "allow_nationwide": self.allow_nationwide,
            "self_delivery_fee": float(self.self_delivery_fee),
            "options": [
                {
                    "code": opt.code,
                    "label": opt.label,
                    "fee": float(opt.fee) if opt.fee is not None else None,
                    "is_free": opt.is_free,
                }
                for opt in self.options
            ],
        }


@dataclass(frozen=True)
class MerchantAccessStatus:
    has_access: bool
    is_subscribed: bool
    is_on_trial: bool
    trial_days_remaining: int
    expires_at: Optional[datetime]
    reason: str  # "active_subscription" | "active_trial" | "trial_expired" | "subscription_expired" | "none"


def evaluate_merchant_subscription_or_trial(
    merchant,
    now: Optional[datetime] = None,
) -> MerchantAccessStatus:
    """
    Single source of truth for checking whether a MerchantProfile has an active
    paid subscription (on MerchantProfile or VendorSubscription) or an active trial.
    """
    now = now or timezone.now()

    # 1. Check active VendorSubscription first
    active_sub = _get_active_vendor_subscription(merchant, now)
    if active_sub is not None:
        return MerchantAccessStatus(
            has_access=True,
            is_subscribed=True,
            is_on_trial=False,
            trial_days_remaining=0,
            expires_at=active_sub.subscription_expires_at,
            reason="active_subscription",
        )

    # 2. Check legacy/direct MerchantProfile subscription flags
    if merchant.is_subscribed:
        if merchant.subscription_expires_at is None or now < merchant.subscription_expires_at:
            return MerchantAccessStatus(
                has_access=True,
                is_subscribed=True,
                is_on_trial=False,
                trial_days_remaining=0,
                expires_at=merchant.subscription_expires_at,
                reason="active_subscription",
            )

    # 3. Check active free trial
    if merchant.trial_ends_at and now < merchant.trial_ends_at:
        delta = merchant.trial_ends_at - now
        days_left = max(1, delta.days + (1 if delta.seconds > 0 else 0))
        return MerchantAccessStatus(
            has_access=True,
            is_subscribed=False,
            is_on_trial=True,
            trial_days_remaining=days_left,
            expires_at=merchant.trial_ends_at,
            reason="active_trial",
        )

    # 4. Determine why access is inactive
    sub_expiry = getattr(getattr(merchant, "vendorsubscription", None), "subscription_expires_at", None)
    effective_expiry = sub_expiry or merchant.subscription_expires_at

    if (merchant.is_subscribed or sub_expiry) and effective_expiry and now >= effective_expiry:
        reason = "subscription_expired"
    elif merchant.trial_ends_at and now >= merchant.trial_ends_at:
        reason = "trial_expired"
    else:
        reason = "none"

    return MerchantAccessStatus(
        has_access=False,
        is_subscribed=False,
        is_on_trial=False,
        trial_days_remaining=0,
        expires_at=effective_expiry or merchant.trial_ends_at,
        reason=reason,
    )


def ensure_merchant_trial_on_approval(merchant, days: int = 7) -> bool:
    """
    Mutates `merchant` in-memory to grant a one-time free trial upon approval
    if the vendor does not already have an active subscription or prior trial.
    Returns True if trial fields were populated.
    """
    if not merchant.is_approved:
        return False

    status = evaluate_merchant_subscription_or_trial(merchant)
    if status.has_access or merchant.has_used_free_trial or merchant.is_subscribed:
        return False

    now = timezone.now()
    merchant.trial_started_at = now
    merchant.trial_ends_at = now + timedelta(days=days)
    merchant.has_used_free_trial = True
    return True


def active_merchant_access_q(now: Optional[datetime] = None) -> Q:
    """
    Reusable ORM Q-filter for public storefront and marketplace queries.
    """
    now = now or timezone.now()
    return (
        Q(is_subscribed=True, subscription_expires_at__isnull=True)
        | Q(is_subscribed=True, subscription_expires_at__gt=now)
        | Q(
            vendorsubscription__is_active=True,
            vendorsubscription__subscription_expires_at__gt=now,
        )
        | Q(trial_ends_at__gt=now)
    )


def get_vendor_checkout_shipping_options(
    vendor,
    same_hub: Optional[bool] = None,
) -> CheckoutShippingOptions:
    """
    Resolves available checkout delivery methods, fees, and nationwide rules
    from a vendor's VendorShippingPreference.

    - `vendor`: MerchantProfile instance (ideally queried with select_related('shipping_preference')).
    - `same_hub`: Optional bool indicating whether the buyer's delivery address is in the
      vendor's hub. When False and `allow_nationwide` is False, platform delivery is excluded
      for platform-only vendors (or restricted accordingly).
    """
    try:
        pref = vendor.shipping_preference
    except (AttributeError, ObjectDoesNotExist):
        pref = None

    fulfillment_type = pref.fulfillment_type if pref else "platform"
    allow_nationwide = bool(pref.allow_nationwide) if pref else False
    self_fee = Decimal(str(pref.self_delivery_fee)) if pref and pref.self_delivery_fee is not None else Decimal("0.00")

    options: List[FulfillmentOption] = []

    platform_enabled = fulfillment_type in ("platform", "hybrid")
    self_enabled = fulfillment_type in ("self", "hybrid")

    # Enforce hub restriction if buyer is outside the vendor's hub and nationwide is disabled
    if same_hub is False and not allow_nationwide:
        platform_enabled = False

    if platform_enabled:
        options.append(
            FulfillmentOption(
                code="platform",
                label="Platform Logistics (Aika Riders)",
                fee=None,  # Calculated dynamically by your Aika distance/hub pricing engine
                is_free=False,
            )
        )

    if self_enabled:
        options.append(
            FulfillmentOption(
                code="self",
                label="Self Delivery (Vendor Handled)",
                fee=self_fee,
                is_free=self_fee <= Decimal("0.00"),
            )
        )

    allowed_methods = [opt.code for opt in options]
    default_method = allowed_methods[0] if allowed_methods else (
        "self" if fulfillment_type == "self" else "platform"
    )

    return CheckoutShippingOptions(
        vendor_id=vendor.id,
        fulfillment_type=fulfillment_type,
        allowed_methods=allowed_methods,
        default_method=default_method,
        allows_selection=len(allowed_methods) > 1,
        allow_nationwide=allow_nationwide,
        self_delivery_fee=self_fee,
        options=options,
    )