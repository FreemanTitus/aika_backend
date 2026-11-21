from decimal import Decimal

from .services.routing import Coordinate, RoutingService

from .models import LogisticsPricingConfig


def calculate_order_delivery_fee(
    restaurant_lat,
    restaurant_lng,
    customer_lat,
    customer_lng,
) -> Decimal:
    """
    Calculates the total delivery fee based on the base fare (₦1,000)
    plus billable road distance (in kilometers) from the restaurant to the customer dropoff.
    """
    # 1. Fetch pricing configuration (or fallback to defaults)
    try:
        config = LogisticsPricingConfig.objects.first()
        base_fare = Decimal(str(config.base_fare)) if config else Decimal("1000.00")
        per_km_rate = (
            Decimal(str(config.per_kilometer_rate)) if config else Decimal("150.00")
        )
    except Exception:
        base_fare = Decimal("1000.00")
        per_km_rate = Decimal("150.00")

    # If coordinates are missing, return default base fare safely
    if (
        restaurant_lat is None
        or restaurant_lng is None
        or customer_lat is None
        or customer_lng is None
    ):
        return quantize_fee(base_fare)

    # 2. Fetch road distance via RoutingService (OSRM with built-in Haversine fallback)
    try:
        origin = Coordinate(lat=float(restaurant_lat), lng=float(restaurant_lng))
        destination = Coordinate(lat=float(customer_lat), lng=float(customer_lng))
        route = RoutingService.get_route(origin, destination, include_geometry=False)
        distance_km = Decimal(str(route.distance_meters)) / Decimal("1000.0")
    except (TypeError, ValueError):
        return quantize_fee(base_fare)

    # 3. Compute final fee: Base Fare + (Billable Distance * Rate per KM)
    included_base_km = Decimal("1.0")
    billable_distance = max(Decimal("0.0"), distance_km - included_base_km)

    total_fee = base_fare + (billable_distance * per_km_rate)
    return quantize_fee(total_fee)


def quantize_fee(amount: Decimal) -> Decimal:
    """Rounds to 2 decimal places."""
    return amount.quantize(Decimal("0.01"))