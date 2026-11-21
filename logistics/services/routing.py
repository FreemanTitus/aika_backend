from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Coordinate:
    lat: float
    lng: float

    def __post_init__(self) -> None:
        if not (-90.0 <= self.lat <= 90.0) or not (-180.0 <= self.lng <= 180.0):
            raise ValueError(f"Invalid coordinate: ({self.lat}, {self.lng})")

    def to_osrm(self) -> str:
        """OSRM expects 'longitude,latitude'."""
        return f"{self.lng:.6f},{self.lat:.6f}"


@dataclass(frozen=True)
class RouteResult:
    distance_meters: float
    duration_seconds: float
    geometry: str | None = None
    is_fallback: bool = False

    @property
    def distance_km(self) -> float:
        return round(self.distance_meters / 1000.0, 2)

    @property
    def duration_minutes(self) -> int:
        return max(1, math.ceil(self.duration_seconds / 60.0))


class RoutingService:
    """
    Client for Aika's self-hosted OSRM engine (127.0.0.1:5000) with
    automatic Haversine fallback so checkout and dispatch never fail.
    """

    _session: requests.Session | None = None

    @classmethod
    def _get_session(cls) -> requests.Session:
        if cls._session is None:
            session = requests.Session()
            retries = Retry(
                total=2,
                backoff_factor=0.1,
                status_forcelist=(502, 503, 504),
                allowed_methods=("GET",),
            )
            adapter = HTTPAdapter(
                pool_connections=10,
                pool_maxsize=20,
                max_retries=retries,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            cls._session = session
        return cls._session

    @staticmethod
    def haversine_meters(origin: Coordinate, destination: Coordinate) -> float:
        """Great-circle distance in meters between two coordinates."""
        earth_radius_m = 6_371_000.0
        lat1, lon1 = math.radians(origin.lat), math.radians(origin.lng)
        lat2, lon2 = math.radians(destination.lat), math.radians(destination.lng)

        dlat = lat2 - lat1
        dlon = lon2 - lon1

        a = (
            math.sin(dlat / 2.0) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
        )
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        return earth_radius_m * c

    @classmethod
    def _fallback_route(cls, points: Sequence[Coordinate]) -> RouteResult:
        """Estimate road distance and duration if OSRM is unreachable or fails."""
        total_straight_m = sum(
            cls.haversine_meters(points[i], points[i + 1])
            for i in range(len(points) - 1)
        )
        detour_factor = getattr(settings, "OSRM_ROAD_DETOUR_FACTOR", 1.35)
        speed_kmh = getattr(settings, "OSRM_FALLBACK_SPEED_KMH", 28.0)

        estimated_distance_m = total_straight_m * detour_factor
        speed_mps = (speed_kmh * 1000.0) / 3600.0
        estimated_duration_s = estimated_distance_m / max(speed_mps, 1.0)

        return RouteResult(
            distance_meters=round(estimated_distance_m, 1),
            duration_seconds=round(estimated_duration_s, 1),
            geometry=None,
            is_fallback=True,
        )

    @classmethod
    def get_route(
        cls,
        origin: Coordinate,
        destination: Coordinate,
        *,
        waypoints: Iterable[Coordinate] = (),
        include_geometry: bool = False,
        profile: str = "driving",
    ) -> RouteResult:
        """
        Calculate road distance, ETA, and optional encoded polyline between
        origin -> [waypoints...] -> destination.
        """
        points = [origin, *waypoints, destination]
        coords_param = ";".join(p.to_osrm() for p in points)

        base_url = getattr(settings, "OSRM_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
        timeout = getattr(settings, "OSRM_TIMEOUT", 3.0)

        url = f"{base_url}/route/v1/{profile}/{coords_param}"
        params = {
            "overview": "simplified" if include_geometry else "false",
            "geometries": "polyline",
            "steps": "false",
        }

        try:
            response = cls._get_session().get(url, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()

            if payload.get("code") != "Ok" or not payload.get("routes"):
                logger.warning(
                    "OSRM returned non-Ok response (%s) for %s; using fallback.",
                    payload.get("code"),
                    coords_param,
                )
                return cls._fallback_route(points)

            route = payload["routes"][0]
            return RouteResult(
                distance_meters=float(route["distance"]),
                duration_seconds=float(route["duration"]),
                geometry=route.get("geometry") if include_geometry else None,
                is_fallback=False,
            )

        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.error("OSRM route lookup failed (%s); falling back to Haversine.", exc)
            return cls._fallback_route(points)

    @classmethod
    def get_distance_matrix(
        cls,
        origins: Sequence[Coordinate],
        destinations: Sequence[Coordinate],
        *,
        profile: str = "driving",
    ) -> list[list[RouteResult]]:
        """
        Compute an N x M matrix of distances and durations in a single OSRM /table
        call (matches your container's --max-table-size 100 setting).
        Ideal for ranking nearby riders or sorting stores by delivery ETA.
        """
        if not origins or not destinations:
            return []

        all_points = [*origins, *destinations]
        if len(all_points) > 100:
            raise ValueError("Total coordinates exceed OSRM --max-table-size (100).")

        coords_param = ";".join(p.to_osrm() for p in all_points)
        sources_idx = ";".join(str(i) for i in range(len(origins)))
        dests_idx = ";".join(
            str(i) for i in range(len(origins), len(origins) + len(destinations))
        )

        base_url = getattr(settings, "OSRM_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
        timeout = getattr(settings, "OSRM_TIMEOUT", 3.0)

        url = f"{base_url}/table/v1/{profile}/{coords_param}"
        params = {
            "sources": sources_idx,
            "destinations": dests_idx,
            "annotations": "distance,duration",
        }

        try:
            response = cls._get_session().get(url, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()

            if payload.get("code") != "Ok":
                raise ValueError(f"OSRM table error: {payload.get('code')}")

            distances = payload["distances"]
            durations = payload["durations"]

            matrix: list[list[RouteResult]] = []
            for r_idx, orig in enumerate(origins):
                row: list[RouteResult] = []
                for c_idx, dest in enumerate(destinations):
                    dist = distances[r_idx][c_idx]
                    dur = durations[r_idx][c_idx]
                    if dist is None or dur is None:
                        row.append(cls._fallback_route([orig, dest]))
                    else:
                        row.append(
                            RouteResult(
                                distance_meters=float(dist),
                                duration_seconds=float(dur),
                                is_fallback=False,
                            )
                        )
                matrix.append(row)
            return matrix

        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.error("OSRM table lookup failed (%s); falling back to Haversine.", exc)
            return [
                [cls._fallback_route([orig, dest]) for dest in destinations]
                for orig in origins
            ]