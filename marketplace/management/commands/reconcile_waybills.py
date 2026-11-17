from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from marketplace.models import MarketplaceOrder
from marketplace.utils import create_logistics_waybill


class Command(BaseCommand):
    help = (
        "Creates the logistics waybill for PAID orders that never got one (for example when "
        "waybill creation failed right after the payment was confirmed). Idempotent: orders that "
        "already have a waybill are left untouched. Run every few minutes."
    )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=2)  # give the normal flow a moment to finish
        candidate_ids = list(
            MarketplaceOrder.objects.filter(
                status=MarketplaceOrder.OrderStatus.PROCESSING,
                fulfillment_mode=MarketplaceOrder.FulfillmentMode.PLATFORM_DISPATCH,
                logistics_dispatch__isnull=True,
                created_at__lt=cutoff,
            ).values_list("id", flat=True)
        )

        created = failed = 0
        for order_id in candidate_ids:
            try:
                with transaction.atomic():
                    order = (
                        MarketplaceOrder.objects.select_for_update(of=("self",))
                        .select_related("vendor", "vendor__user", "customer", "customer__user")
                        .get(id=order_id)
                    )
                    if order.logistics_dispatch_id or order.status != MarketplaceOrder.OrderStatus.PROCESSING:
                        continue  # someone else fixed it meanwhile
                    create_logistics_waybill(order)
                    created += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f"Order {order_id}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"Orders missing a waybill: {len(candidate_ids)}, created: {created}, failed: {failed}"
        ))
