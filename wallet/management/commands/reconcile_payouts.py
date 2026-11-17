from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from wallet.models import PayoutLog
from wallet.services import reconcile_payout


class Command(BaseCommand):
    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(minutes=5)
        refs = PayoutLog.objects.filter(
            status=PayoutLog.PayoutStatus.PROCESSING, created_at__lt=cutoff
        ).values_list("reference", flat=True)
        for ref in refs:
            reconcile_payout(ref)