from .models import RideBooking, SendPackage


class BookingService:
    @staticmethod
    def create_ride(data, user=None) -> RideBooking:
        return RideBooking.objects.create(
            user=user,
            customer_name=data.customer_name,
            customer_phone=data.customer_phone,
            pickup_address=data.pickup_address,
            dropoff_address=data.dropoff_address,
            fare_amount=data.fare_amount
        )

    @staticmethod
    def create_package_delivery(data, user=None) -> SendPackage:
        return SendPackage.objects.create(
            user=user,
            customer_name=data.customer_name,
            customer_phone=data.customer_phone,
            pickup_address=data.pickup_address,
            dropoff_address=data.dropoff_address,
            fare_amount=data.fare_amount,
            receiver_name=data.receiver_name,
            receiver_phone=data.receiver_phone,
            package_description=data.package_description,
            is_cash_on_delivery=data.is_cash_on_delivery,
            cod_amount=data.cod_amount
        )
    