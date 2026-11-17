from ninja import Schema


class LocationUpdateSchema(Schema):
    latitude: float
    longitude: float
    