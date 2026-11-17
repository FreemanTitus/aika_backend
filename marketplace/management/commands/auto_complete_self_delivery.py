from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from marketplace.models import MarketplaceOrder
from order.services import complete_marketplace_order


class Command(BaseCommand):
    help = (
        "Completes vendor-delivered orders that the vendor marked delivered but the customer never "
        "confirmed, after SELF_DELIVERY_AUTO_CONFIRM_HOURS, and pays the vendor. Run it hourly."
    )

    def handle(self, *args, **options):
        hours = getattr(settings, "SELF_DELIVERY_AUTO_CONFIRM_HOURS", 48)
        cutoff = timezone.now() - timedelta(hours=hours)

        order_ids = list(
            MarketplaceOrder.objects.filter(
                fulfillment_mode=MarketplaceOrder.FulfillmentMode.VENDOR_SELF_DELIVERY,
                status=MarketplaceOrder.OrderStatus.IN_TRANSIT,
                vendor_marked_delivered_at__isnull=False,
                vendor_marked_delivered_at__lt=cutoff,
            ).values_list("id", flat=True)
        )

        completed = failed = 0
        for order_id in order_ids:
            try:
                complete_marketplace_order(order_id, None, system=True)
                completed += 1
            except (LookupError, ValueError) as exc:
                failed += 1
                self.stderr.write(f"Order {order_id}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"Self-delivery orders past {hours}h: {len(order_ids)}, auto-completed: {completed}, skipped: {failed}"
        ))
