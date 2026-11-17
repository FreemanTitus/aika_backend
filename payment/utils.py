import uuid
import httpx
from django.conf import settings
from asgiref.sync import sync_to_async


async def create_paystack_subaccount_for_vendor(vendor_profile, user, bank_code, account_number, business_name):
    url = "https://api.paystack.co/subaccount"
    
    payload = {
        "business_name": business_name,
        "bank_code": bank_code,
        "account_number": account_number,
        "percentage_charge": 5.0  # Example: Platform takes 5% commission, 95% goes to vendor
    }
    
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=15.0)
        res_data = response.json()
        
        if response.status_code in [200, 201] and res_data.get("status"):
            subaccount_code = res_data["data"]["subaccount_code"]
            
            # Save this code to your database model
            vendor_profile.paystack_subaccount_code = subaccount_code
            await sync_to_async(vendor_profile.save)(update_fields=['paystack_subaccount_code'])
            return subaccount_code
        else:
            raise Exception(f"Failed to create subaccount: {res_data.get('message')}")