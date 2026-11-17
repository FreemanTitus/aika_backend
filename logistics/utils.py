import math
from decimal import Decimal
from .models import LogisticsPricingConfig


def calculate_order_delivery_fee(restaurant_lat, restaurant_lng, customer_lat, customer_lng) -> Decimal:
    """
    Calculates the total delivery fee based on the base fare (₦1,000) 
    plus distance (in kilometers) from the restaurant to the customer dropoff.
    """
    # 1. Fetch pricing configuration (or fallback to model defaults)
    try:
        config = LogisticsPricingConfig.objects.first()
        base_fare = config.base_fare if config else Decimal('1000.00')
        per_km_rate = config.per_kilometer_rate if config else Decimal('150.00')
    except Exception:
        base_fare = Decimal('1000.00')
        per_km_rate = Decimal('150.00')

    # If coordinates are missing, return default base fare safely
    if not restaurant_lat or not restaurant_lng or not customer_lat or not customer_lng:
        return base_fare

    # 2. Calculate distance in kilometers using the Haversine formula
    lat1, lon1 = radians(float(restaurant_lat)), radians(float(restaurant_lng))
    lat2, lon2 = radians(float(customer_lat)), radians(float(customer_lng))

    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    
    # Earth radius in kilometers approx 6371
    distance_km = Decimal(str(6371 * c))

    # 3. Compute final fee: Base Fare + (Distance * Rate per KM)
    # You can optionally subtract a base distance threshold (e.g. first 1 or 2 km covered by base fare)
    included_base_km = Decimal('1.0') 
    billable_distance = max(Decimal('0.0'), distance_km - included_base_km)

    total_fee = base_fare + (billable_distance * per_km_rate)

    # Round up to the nearest 50 or 100 Naira if preferred
    return quantize_fee(total_fee)

def radians(deg):
    return deg * (math.pi / 180)

def sin(x):
    return math.sin(x)

def cos(x):
    return math.cos(x)

def atan2(y, x):
    return math.atan2(y, x)

def sqrt(x):
    return math.sqrt(x)

def quantize_fee(amount: Decimal) -> Decimal:
    # Rounds to 2 decimal places
    return amount.quantize(Decimal('0.01'))