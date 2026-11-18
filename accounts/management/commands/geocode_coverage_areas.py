from django.core.management.base import BaseCommand
from django.db.models import Q

from accounts.models import CoverageArea


class Command(BaseCommand):
    help = (
        "Fills in latitude/longitude for coverage areas that have none (used to sanity-check customer "
        "coordinates at checkout). Run it once after adding areas; it is rate-limited to 1 request/second."
    )

    def handle(self, *args, **options):
        areas = CoverageArea.objects.select_related("state").filter(
            Q(latitude__isnull=True) | Q(longitude__isnull=True)
        )
        done = failed = 0
        for area in areas:
            if area.geocode():
                done += 1
                self.stdout.write(f"  {area}: {area.latitude}, {area.longitude}")
            else:
                failed += 1
                self.stderr.write(f"  {area}: not found")
        self.stdout.write(self.style.SUCCESS(f"Geocoded {done}, could not find {failed}"))
