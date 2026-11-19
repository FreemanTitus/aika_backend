import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.mail import send_mail
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from payment.models import SubscriptionReminderLog, VendorSubscription

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Send upcoming expiration and renewal reminders to vendors."

    def handle(self, *args, **options):
        now = timezone.now()
        active_subs = VendorSubscription.objects.select_related(
            'vendor_profile__user', 'plan'
        ).filter(is_active=True, subscription_expires_at__isnull=False)

        thresholds = [
            (SubscriptionReminderLog.ReminderType.EXPIRING_1_DAY, timedelta(days=1)),
            (SubscriptionReminderLog.ReminderType.EXPIRING_3_DAYS, timedelta(days=3)),
            (SubscriptionReminderLog.ReminderType.EXPIRING_7_DAYS, timedelta(days=7)),
        ]

        for sub in active_subs:
            expires_at = sub.subscription_expires_at
            remaining = expires_at - now

            if remaining <= timedelta(0):
                self._dispatch_once(
                    sub,
                    SubscriptionReminderLog.ReminderType.EXPIRED,
                    "Your Aika subscription has expired",
                    f"Your {sub.plan.name if sub.plan else ''} plan expired. Renew now to keep accepting orders.",
                )
                continue

            for reminder_type, max_delta in thresholds:
                if remaining <= max_delta:
                    days_left = max(1, remaining.days)
                    self._dispatch_once(
                        sub,
                        reminder_type,
                        f"Aika subscription expires in {days_left} day(s)",
                        f"Your {sub.plan.name if sub.plan else ''} subscription expires on {expires_at:%Y-%m-%d}. Renew early to avoid interruption.",
                    )
                    break

    def _dispatch_once(self, sub, reminder_type, title, message):
        if SubscriptionReminderLog.objects.filter(
            subscription=sub,
            reminder_type=reminder_type,
            cycle_expires_at=sub.subscription_expires_at,
        ).exists():
            return

        user = sub.vendor_profile.user
        recipient_email = getattr(user, 'email', None)
        delivered = False

        # 1. Real-time WebSocket alert (if vendor is online)
        channel_layer = get_channel_layer()
        if channel_layer is not None:
            try:
                # Match your existing vendor consumer group name (e.g. f"vendor_{sub.vendor_profile.id}" or f"user_{user.id}")
                group_name = f"vendor_{sub.vendor_profile.id}"
                async_to_sync(channel_layer.group_send)(
                    group_name,
                    {
                        "type": "subscription_reminder",  # Must match a handler method on your Channels consumer
                        "event": "subscription_reminder",
                        "reminder_type": reminder_type,
                        "title": title,
                        "message": message,
                        "expires_at": sub.subscription_expires_at.isoformat(),
                        "url": "/vendor/subscription",
                    },
                )
                delivered = True
            except Exception as exc:
                logger.warning("WebSocket send failed for vendor %s: %s", sub.vendor_profile.id, exc)

        # 2. Email fallback / permanent record
        if recipient_email:
            try:
                send_mail(
                    subject=title,
                    message=(
                        f"Hello {getattr(sub.vendor_profile, 'business_name', None) or recipient_email},\n\n"
                        f"{message}\n\n"
                        f"Manage your subscription: https://www.useaika.ng/vendor/subscription\n\n"
                        f"— Aika Team"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient_email],
                    fail_silently=False,
                )
                delivered = True
            except Exception as exc:
                logger.exception("Email send failed for %s: %s", recipient_email, exc)

        if not delivered:
            return

        SubscriptionReminderLog.objects.create(
            subscription=sub,
            reminder_type=reminder_type,
            cycle_expires_at=sub.subscription_expires_at,
        )
        self.stdout.write(f"Sent {reminder_type} to {recipient_email or sub.vendor_profile.id}")