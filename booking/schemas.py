from ninja import Schema
from typing import Optional

class RideBookingInput(Schema):
    customer_name: str
    customer_phone: str
    pickup_address: str
    dropoff_address: str
    fare_amount: float


class SendPackageInput(Schema):
    customer_name: str
    customer_phone: str
    pickup_address: str
    dropoff_address: str
    fare_amount: float
    receiver_name: str
    receiver_phone: str
    package_description: str
    is_cash_on_delivery: bool = False
    cod_amount: float = 0.00