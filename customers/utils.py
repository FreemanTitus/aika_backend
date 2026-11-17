import uuid
from .models import IndividualProfile, Referral

def handle_individual_registration(user, data):
    # 1. Extract and clean referral code
    referral_code_input = str(data.get('referral_code', '')).strip()
    inviter_user = None

    if referral_code_input:
        try:
            # Locate inviter's profile using the unique referral code
            inviter_profile = IndividualProfile.objects.get(referral_code__iexact=referral_code_input)
            inviter_user = inviter_profile.user
        except IndividualProfile.DoesNotExist:
            pass  # Invalid codes can be gracefully ignored or handled based on your preference

    # 2. Create Individual Profile (referral_code is auto-generated on save())
    profile = IndividualProfile.objects.create(
        id=f"CUS-{uuid.uuid4().hex[:8].upper()}",
        user=user,
        first_name=data.get('first_name'),
        last_name=data.get('last_name'),
        default_delivery_address=data.get('street_address', ''),
        referred_by=inviter_user # Optional field on profile model if you kept it
    )

    # 3. Create dedicated Referral tracking record if inviter exists and isn't the same user
    if inviter_user and inviter_user != user:
        Referral.objects.create(
            inviter=inviter_user,
            invitee=user
        )

    return profile