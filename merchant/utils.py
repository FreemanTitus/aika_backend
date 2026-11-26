from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from django.db.models import Q
from django.utils import timezone


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
    paid subscription or an active 7-day free trial.
    """
    now = now or timezone.now()

    # 1. Check paid subscription first
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

    # 2. Check active free trial
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

    # 3. Determine why access is inactive
    if merchant.is_subscribed and merchant.subscription_expires_at and now >= merchant.subscription_expires_at:
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
        expires_at=merchant.subscription_expires_at or merchant.trial_ends_at,
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