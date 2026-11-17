import os
import httpx
from datetime import datetime, timedelta
from django.conf import settings
import uuid


class AuthManager:
    def __init__(self):
        self.client_id = getattr(settings, 'FLW_TEST_CLIENT_ID', None)
        self.client_secret = getattr(settings, 'FLW_TEST_CLIENT_SECRET', None)
        self.access_token = None
        self.expiry = None

    async def generate_access_token(self):
        url = 'https://idp.flutterwave.com/realms/flutterwave/protocol/openid-connect/token'
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url=url, headers=headers, data=data, timeout=10.0)
            
            if response.status_code == 200:
                response_json = response.json()
                self.access_token = response_json.get("access_token")
                
                # Flutterwave tokens expire in 10 minutes (600 seconds)
                expires_in = response_json.get("expires_in", 600)
                self.expiry = datetime.now() + timedelta(seconds=expires_in)
            else:
                print(f"DEBUG: AuthManager IDP Error: {response.text}")
                raise Exception(f"Failed to handshake with Flutterwave IDP: {response.text}")

    async def get_access_token(self):
        # Generate token if missing, or refresh if it expires in less than 1 minute
        if not self.access_token or not self.expiry or (self.expiry - datetime.now()) < timedelta(minutes=1):
            await self.generate_access_token()
            
        return self.access_token

flw_auth = AuthManager()



async def register_v4_payment_method(method_type: str = "bank_account"):
    """
    Registers or fetches payment methods using Flutterwave v4 API.
    """
    url = "https://developersandbox-api.flutterwave.com/payment-methods"
    
    # 1. Fetch dynamic OAuth Bearer token
    access_token = await flw_auth.get_access_token()
    
    # 2. Set up v4 headers including tracing
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {access_token}",
        "X-Trace-Id": uuid.uuid4().hex
    }
    
    # 3. Payload configuration
    payload = {
        "type": method_type
    }

    # 4. Execute request
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            response_data = response.json()
            
            print(f"FULL V4 PAYMENT METHOD RESPONSE: {response_data}")
            
            if response.status_code in [200, 201]:
                return response_data
            else:
                raise Exception(f"Failed to process payment method: {response_data}")
                
        except httpx.RequestError as e:
            raise Exception(f"Gateway connection error: {str(e)}")