from __future__ import annotations

import math
from typing import Optional


# Anchor points recorded from the robot's GPS receiver while physically
# walking the patrol route.
GPS_ROUTE: list[tuple[float, float]] = [
    (43.77134704, -79.50576782),
    (43.77128219, -79.50572967),
    (43.77111816, -79.50568389),
    (43.77092361, -79.50556182),
    (43.77082443, -79.50547027),
    (43.77059555, -79.50537872),
    (43.77059555, -79.50531768),
    (43.77075195, -79.50537872),
    (43.77087402, -79.50542449),
    (43.77111816, -79.50553131),
    (43.77127456, -79.50557708),
    (43.77141189, -79.50563812),
    (43.77149963, -79.50569152),
]

# Corresponding reference positions extracted from Google Earth KML.
# Each index i maps to the same physical location as GPS_ROUTE[i].
REF_ROUTE: list[tuple[float, float]] = [
    (43.77141070785147, -79.50576340335775),
    (43.77128396691195, -79.50570137649684),
    (43.77111579317859, -79.50563621046172),
    (43.77090099240406, -79.50554361201812),
    (43.77061834632729, -79.50541589732718),
    (43.77056004780789, -79.50538669569928),
    (43.77054264317429, -79.50537894261889),
    (43.77054266824787, -79.50533361096626),
    (43.77056084863841, -79.50532417527429),
    (43.77057653984411, -79.50533272396387),
    (43.77083228856302, -79.50543972711313),
    (43.77119166271184, -79.50558822038171),
    (43.77127445397785, -79.50562194485286),
]

# Raw GPS readings more than this many metres from the nearest route segment
# are not remapped — they are returned unchanged. This prevents placeholder
# or indoor GPS values from being incorrectly projected onto the route.
MAX_CALIBRATION_DISTANCE_M: float = 50.0


def _latlon_to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    R = 6_371_000.0
    x = math.radians(lon - lon0) * R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * R
    return x, y


def _xy_to_latlon(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    R = 6_371_000.0
    lat = lat0 + math.degrees(y / R)
    lon = lon0 + math.degrees(x / (R * math.cos(math.radians(lat0))))
    return lat, lon


class SegmentMapper:
    """Maps a raw GPS coordinate to a corrected coordinate by projecting it
    onto the nearest segment of a known robot route, then applying the same
    interpolation factor to the corresponding reference-route segment.

    This corrects the systematic offset between what the robot's GPS reports
    and the true physical positions derived from Google Earth.
    """

    def __init__(
        self,
        gps_route: list[tuple[float, float]],
        ref_route: list[tuple[float, float]],
        max_distance_m: float = MAX_CALIBRATION_DISTANCE_M,
    ) -> None:
        if len(gps_route) != len(ref_route):
            raise ValueError("gps_route and ref_route must have the same length")
        if len(gps_route) < 2:
            raise ValueError("Routes must have at least 2 points")

        self._lat0, self._lon0 = ref_route[0]
        self._max_dist_m = max_distance_m
        self._gps_xy = [_latlon_to_xy(lat, lon, self._lat0, self._lon0) for lat, lon in gps_route]
        self._ref_xy = [_latlon_to_xy(lat, lon, self._lat0, self._lon0) for lat, lon in ref_route]

    def _project_to_segment(
        self,
        p: tuple[float, float],
        a: tuple[float, float],
        b: tuple[float, float],
    ) -> tuple[tuple[float, float], float]:
        ab = (b[0] - a[0], b[1] - a[1])
        ap = (p[0] - a[0], p[1] - a[1])
        ab_len2 = ab[0] * ab[0] + ab[1] * ab[1]
        if ab_len2 == 0.0:
            return a, 0.0
        t = (ap[0] * ab[0] + ap[1] * ab[1]) / ab_len2
        t = max(0.0, min(1.0, t))
        proj = (a[0] + ab[0] * t, a[1] + ab[1] * t)
        return proj, t

    def _find_best_segment(self, p: tuple[float, float]) -> tuple[int, float, float]:
        best_dist = float("inf")
        best_idx = 0
        best_t = 0.0
        for i in range(len(self._gps_xy) - 1):
            proj, t = self._project_to_segment(p, self._gps_xy[i], self._gps_xy[i + 1])
            d = math.hypot(p[0] - proj[0], p[1] - proj[1])
            if d < best_dist:
                best_dist = d
                best_idx = i
                best_t = t
        return best_idx, best_t, best_dist

    def map_point(self, lat: float, lon: float) -> Optional[tuple[float, float]]:
        """Return the calibrated (lat, lon) for a raw GPS reading.

        Returns None if the point is too far from the route to be reliably
        mapped (e.g. placeholder coordinates or readings taken far from the lot).
        """
        p = _latlon_to_xy(lat, lon, self._lat0, self._lon0)
        seg_idx, t, dist_m = self._find_best_segment(p)
        if dist_m > self._max_dist_m:
            return None
        ref_a = self._ref_xy[seg_idx]
        ref_b = self._ref_xy[seg_idx + 1]
        mapped_xy = (
            ref_a[0] + (ref_b[0] - ref_a[0]) * t,
            ref_a[1] + (ref_b[1] - ref_a[1]) * t,
        )
        return _xy_to_latlon(mapped_xy[0], mapped_xy[1], self._lat0, self._lon0)


default_mapper = SegmentMapper(GPS_ROUTE, REF_ROUTE)
