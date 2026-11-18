# your_app/management/commands/seed_locations.py
from django.core.management.base import BaseCommand
from accounts.models import State, CoverageArea  # Adjust import to your app name

class Command(BaseCommand):
    help = 'Automatically seeds initial Nigerian States and Coverage Area hubs.'

    def handle(self, *args, **options):
        self.stdout.write("Seeding regional states and coverage hubs...")

        # Define your initial dataset
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
            "Nasarawa": ["Lafia", "Akwanga", "Nasarawa Eggon", "Keffi", "Karu", "Mararaba", "Masaka", "Wamba", "Doma", "Awe", "Keana", "Kokona", "Nasarawa", "Obi", "Toto"],
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
            "FCT": ["Abuja (AMAC)", "Gwarinpa", "Wuse", "Kubwa", "Lugbe", "Garki", "Asokoro", "Maitama", "Bwari", "Kuje", "Gwagwalada"]
        }

        for state_name, hubs in initial_data.items():
            state_obj, created = State.objects.get_or_create(name=state_name)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created State: {state_name}"))
            else:
                self.stdout.write(f"State already exists: {state_name}")

            for hub_name in hubs:
                hub_obj, hub_created = CoverageArea.objects.get_or_create(
                    state=state_obj,
                    name=hub_name
                )
                if hub_created:
                    self.stdout.write(self.style.SUCCESS(f"  -> Added Hub: {hub_name}, {state_name} (Coords: {hub_obj.latitude}, {hub_obj.longitude})"))
                else:
                    self.stdout.write(f"  -> Hub already exists: {hub_name}")

        self.stdout.write(self.style.SUCCESS("Location seeding completed successfully! Your model's geolocator automatically fetched coordinates."))