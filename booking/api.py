from ninja import Router
from accounts.utils import JWTAuth
from .services import BookingService
from .schemas import RideBookingInput, SendPackageInput

router = Router(
    tags=["Booking"],
     auth=JWTAuth()
)

@router.post("/ride/create")
def create_ride_api(request, data: RideBookingInput):
    """
    Validates and stores a new passenger ride booking.
    """
    try:
        booking = BookingService.create_ride(data, user=request.user)
        return {
            "status": "success",
            "booking_reference": booking.booking_reference,
            "booking_status": booking.status
        }
    except ValueError as e:
        return 400, {"status": "error", "message": str(e)}
    except Exception as e:
        return 500, {"status": "error", "message": "An unexpected error occurred"}


@router.post("/delivery/create")
def create_delivery_api(request, data: SendPackageInput):
    """
    Validates and stores a new package delivery booking.
    """
    try:
        booking = BookingService.create_package_delivery(data, user=request.user)
        return {
            "status": "success",
            "booking_reference": booking.booking_reference,
            "booking_status": booking.status
        }
    except ValueError as e:
        return 400, {"status": "error", "message": str(e)}
    except Exception as e:
        return 500, {"status": "error", "message": "An unexpected error occurred"}