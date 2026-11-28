# accounts/management/commands/seed_locations.py
from django.core.management.base import BaseCommand
from accounts.models import State, CoverageArea  # Adjust import to your app name


class Command(BaseCommand):
    help = (
        "Seeds Nigerian States and CoverageArea hubs with Akwanga (Nasarawa) "
        "enabled for merchant and rider registration."
    )

    MERCHANT_ELIGIBLE_HUBS = {
        ("Nasarawa", "Akwanga"),
    }

    RIDER_ELIGIBLE_HUBS = {
        ("Nasarawa", "Akwanga"),
    }

    def handle(self, *args, **options):
        self.stdout.write("Seeding regional states and coverage hubs...")

        initial_data = {
            "Abia": ["Umuahia", "Aba"],
            "Adamawa": ["Yola", "Mubi", "Jimeta"],
            "Akwa Ibom": ["Uyo", "Eket", "Ikot Ekpene"],
            "Anambra": ["Awka", "Onitsha", "Nnewi"],
            "Bauchi": ["Bauchi", "Azare"],
            "Bayelsa": ["Yenagoa"],
            "Benue": ["Makurdi", "Gboko", "Otukpo"],
            "Borno": ["Maiduguri", "Biu"],
            "Cross River": ["Calabar", "Ikom", "Ogoja"],
            "Delta": ["Asaba", "Warri", "Sapele", "Ughelli"],
            "Ebonyi": ["Abakaliki"],
            "Edo": ["Benin City", "Auchi", "Ekpoma"],
            "Ekiti": ["Ado Ekiti", "Ikole"],
            "Enugu": ["Enugu", "Nsukka"],
            "Gombe": ["Gombe"],
            "Imo": ["Owerri", "Orlu", "Okigwe"],
            "Jigawa": ["Dutse", "Hadejia"],
            "Kaduna": ["Kaduna", "Zaria", "Kafanchan"],
            "Kano": ["Kano", "Wudil", "Gwarzo"],
            "Katsina": ["Katsina", "Funtua", "Daura"],
            "Kebbi": ["Birnin Kebbi", "Argungu", "Yauri"],
            "Kogi": ["Lokoja", "Okene", "Kabba", "Idah"],
            "Kwara": ["Ilorin", "Offa", "Omu-Aran"],
            "Lagos": ["Ikeja", "Lagos Island", "Lekki", "Ikorodu", "Epe", "Badagry"],
            "Nasarawa": [
                "Lafia", "Akwanga", "Nasarawa Eggon", "Keffi", "Karu",
                "Mararaba", "Masaka", "Wamba", "Doma", "Awe",
                "Keana", "Kokona", "Nasarawa", "Obi", "Toto",
            ],
            "Niger": ["Minna", "Bida", "Suleja", "Kontagora"],
            "Ogun": ["Abeokuta", "Sagamu", "Ijebu Ode", "Ota", "Ilaro"],
            "Ondo": ["Akure", "Ondo City", "Owo", "Ikare"],
            "Osun": ["Osogbo", "Ile-Ife", "Ilesa", "Ede"],
            "Oyo": ["Ibadan", "Ogbomoso", "Oyo Town", "Iseyin"],
            "Plateau": ["Jos", "Bukuru", "Pankshin", "Shendam"],
            "Rivers": ["Port Harcourt", "Obio/Akpor", "Bonny", "Okrika", "Eleme"],
            "Sokoto": ["Sokoto"],
            "Taraba": ["Jalingo", "Wukari", "Gembu"],
            "Yobe": ["Damaturu", "Potiskum", "Gashua"],
            "Zamfara": ["Gusau", "Kaura Namoda"],
            "FCT": [
                "Abuja (AMAC)", "Gwarinpa", "Wuse", "Kubwa", "Lugbe",
                "Garki", "Asokoro", "Maitama", "Bwari", "Kuje", "Gwagwalada",
            ],
        }

        state_fields = {f.name for f in State._meta.get_fields()}
        area_fields = {f.name for f in CoverageArea._meta.get_fields()}

        for state_name, hubs in initial_data.items():
            state_defaults = {}
            if "is_active" in state_fields:
                state_defaults["is_active"] = True

            state_obj, created = State.objects.get_or_create(
                name=state_name,
                defaults=state_defaults,
            )
            if not created and "is_active" in state_fields and not state_obj.is_active:
                state_obj.is_active = True
                state_obj.save(update_fields=["is_active"])

            if created:
                self.stdout.write(self.style.SUCCESS(f"Created State: {state_name}"))
            else:
                self.stdout.write(f"State already exists: {state_name}")

            for hub_name in hubs:
                pair = (state_name, hub_name)
                allow_merchant = pair in self.MERCHANT_ELIGIBLE_HUBS
                allow_rider = pair in self.RIDER_ELIGIBLE_HUBS

                hub_defaults = {}
                if "is_active" in area_fields:
                    hub_defaults["is_active"] = True
                if "allows_merchant_registration" in area_fields:
                    hub_defaults["allows_merchant_registration"] = allow_merchant
                if "allows_rider_registration" in area_fields:
                    hub_defaults["allows_rider_registration"] = allow_rider

                hub_obj, hub_created = CoverageArea.objects.get_or_create(
                    state=state_obj,
                    name=hub_name,
                    defaults=hub_defaults,
                )

                if not hub_created:
                    fields_to_update = []
                    if "is_active" in area_fields and not hub_obj.is_active:
                        hub_obj.is_active = True
                        fields_to_update.append("is_active")
                    if (
                        "allows_merchant_registration" in area_fields
                        and hub_obj.allows_merchant_registration != allow_merchant
                    ):
                        hub_obj.allows_merchant_registration = allow_merchant
                        fields_to_update.append("allows_merchant_registration")
                    if (
                        "allows_rider_registration" in area_fields
                        and hub_obj.allows_rider_registration != allow_rider
                    ):
                        hub_obj.allows_rider_registration = allow_rider
                        fields_to_update.append("allows_rider_registration")

                    if fields_to_update:
                        hub_obj.save(update_fields=fields_to_update)

                status_tag = (
                    " [MERCHANT + RIDER OPEN]"
                    if (allow_merchant and allow_rider)
                    else ""
                )
                if hub_created:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  -> Added Hub: {hub_name}, {state_name}{status_tag} "
                            f"(Coords: {hub_obj.latitude}, {hub_obj.longitude})"
                        )
                    )
                else:
                    self.stdout.write(f"  -> Hub already exists: {hub_name}{status_tag}")

        self.stdout.write(
            self.style.SUCCESS(
                "Location seeding completed successfully! Only Akwanga (Nasarawa) "
                "is enabled for merchant and rider registration."
            )
        )