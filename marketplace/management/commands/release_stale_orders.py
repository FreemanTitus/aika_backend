from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from marketplace.models import MarketplaceOrder
from marketplace.utils import release_unpaid_order
from wallet.utils import PAID_ORDER_STATES, verify_order_payment


class Command(BaseCommand):
    help = (
        "Releases PENDING marketplace orders that were never paid: restores the reserved stock "
        "and refunds any wallet credit applied at checkout. Run it every ~10 minutes "
        "(cron / Celery beat)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes", type=int, default=None,
            help="Override PENDING_ORDER_TTL_MINUTES for this run.",
        )

    def handle(self, *args, **options):
        ttl = options["minutes"] or getattr(settings, "PENDING_ORDER_TTL_MINUTES", 120)
        cutoff = timezone.now() - timedelta(minutes=ttl)

        stale_ids = list(
            MarketplaceOrder.objects.filter(
                status=MarketplaceOrder.OrderStatus.PENDING, created_at__lt=cutoff
            ).values_list("id", "payment_reference")
        )

        released = settled = 0
        for order_id, reference in stale_ids:
            # The customer may have paid and our webhook may simply have been lost:
            # confirm with Paystack BEFORE cancelling so a paid order is never released.
            if reference:
                try:
                    if verify_order_payment(str(order_id), transaction_id=reference):
                        settled += 1
                        continue
                except Exception as exc:  # network trouble: leave it for the next run
                    self.stderr.write(f"Skipped {order_id}: {exc}")
                    continue
            if release_unpaid_order(order_id):
                released += 1

        self.stdout.write(self.style.SUCCESS(
            f"Stale orders checked: {len(stale_ids)}, released: {released}, found paid: {settled}"
        ))
