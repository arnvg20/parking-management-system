from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path


Coordinate = tuple[float, float]
EARTH_RADIUS_METERS = 6371000
KML_NAMESPACE = {"kml": "http://www.opengis.net/kml/2.2"}

# BN-880 anchor points captured from the robot. The route order must match the
# Google Earth reference route so segment-by-segment interpolation stays stable.
DEFAULT_GPS_ROUTE: list[Coordinate] = [
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

# Latest Google Earth anchor points derived from the matched KML route.
DEFAULT_REF_ROUTE: list[Coordinate] = [
    (43.77141297568659, -79.50576188423069),
    (43.77130647508243, -79.50571215307507),
    (43.77111310959083, -79.50563481932696),
    (43.77090279732915, -79.50555252968286),
    (43.77076752865667, -79.50549008578955),
    (43.77055272656943, -79.50539245007538),
    (43.77056509260502, -79.50532792306956),
    (43.77071936914739, -79.5053934485848),
    (43.77083141278867, -79.50544028876637),
    (43.77108583798839, -79.50554203035357),
    (43.77124637316517, -79.5056022784437),
    (43.77138225040423, -79.50565968809826),
    (43.77147409869628, -79.50569496753968),
]


def latlon_to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    x_value = math.radians(lon - lon0) * EARTH_RADIUS_METERS * math.cos(math.radians(lat0))
    y_value = math.radians(lat - lat0) * EARTH_RADIUS_METERS
    return x_value, y_value


def xy_to_latlon(x_value: float, y_value: float, lat0: float, lon0: float) -> Coordinate:
    latitude = lat0 + math.degrees(y_value / EARTH_RADIUS_METERS)
    longitude = lon0 + math.degrees(x_value / (EARTH_RADIUS_METERS * math.cos(math.radians(lat0))))
    return latitude, longitude


def dist(a_point: tuple[float, float], b_point: tuple[float, float]) -> float:
    return math.hypot(b_point[0] - a_point[0], b_point[1] - a_point[1])


def dot(a_point: tuple[float, float], b_point: tuple[float, float]) -> float:
    return a_point[0] * b_point[0] + a_point[1] * b_point[1]


def sub(a_point: tuple[float, float], b_point: tuple[float, float]) -> tuple[float, float]:
    return a_point[0] - b_point[0], a_point[1] - b_point[1]


def add(a_point: tuple[float, float], b_point: tuple[float, float]) -> tuple[float, float]:
    return a_point[0] + b_point[0], a_point[1] + b_point[1]


def scale(vector: tuple[float, float], scalar: float) -> tuple[float, float]:
    return vector[0] * scalar, vector[1] * scalar


def load_ref_route_from_kml(kml_path: str | Path) -> list[Coordinate]:
    document = ET.parse(Path(kml_path))
    coordinate_nodes = document.findall(".//kml:Placemark/kml:LineString/kml:coordinates", KML_NAMESPACE)

    for coordinate_node in coordinate_nodes:
        raw_coordinates = (coordinate_node.text or "").strip()
        if not raw_coordinates:
            continue

        route: list[Coordinate] = []
        for coordinate_group in raw_coordinates.split():
            parts = coordinate_group.split(",")
            if len(parts) < 2:
                continue
            longitude = float(parts[0])
            latitude = float(parts[1])
            route.append((latitude, longitude))

        if len(route) >= 2:
            return route

    raise ValueError(f"No usable LineString route found in {kml_path}")


def build_segment_mapper(
    gps_route: list[Coordinate] | None = None,
    ref_route: list[Coordinate] | None = None,
    ref_route_kml_path: str | Path | None = None,
) -> "SegmentMapper":
    source_gps_route = list(gps_route or DEFAULT_GPS_ROUTE)
    source_ref_route = list(ref_route or DEFAULT_REF_ROUTE)

    if ref_route_kml_path:
        try:
            kml_ref_route = load_ref_route_from_kml(ref_route_kml_path)
        except (ET.ParseError, OSError, ValueError):
            kml_ref_route = None
        if kml_ref_route and len(kml_ref_route) == len(source_gps_route):
            source_ref_route = kml_ref_route

    return SegmentMapper(source_gps_route, source_ref_route)


class SegmentMapper:
    def __init__(self, gps_route: list[Coordinate], ref_route: list[Coordinate]):
        if len(gps_route) < 2 or len(ref_route) < 2:
            raise ValueError("SegmentMapper requires at least two anchor points per route")
        if len(gps_route) != len(ref_route):
            raise ValueError("gps_route and ref_route must contain the same number of points")

        self.lat0, self.lon0 = ref_route[0]
        self.gps_xy = [latlon_to_xy(lat, lon, self.lat0, self.lon0) for lat, lon in gps_route]
        self.ref_xy = [latlon_to_xy(lat, lon, self.lat0, self.lon0) for lat, lon in ref_route]

    def project_to_segment(
        self,
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[tuple[float, float], float]:
        segment_vector = sub(end, start)
        point_vector = sub(point, start)
        segment_length_squared = dot(segment_vector, segment_vector)
        if segment_length_squared == 0:
            return start, 0.0

        interpolation = dot(point_vector, segment_vector) / segment_length_squared
        interpolation = max(0.0, min(1.0, interpolation))
        projected = add(start, scale(segment_vector, interpolation))
        return projected, interpolation

    def find_segment(self, point: tuple[float, float]) -> tuple[int, float, float]:
        best_distance = float("inf")
        best_index = 0
        best_interpolation = 0.0

        for index in range(len(self.gps_xy) - 1):
            start = self.gps_xy[index]
            end = self.gps_xy[index + 1]
            projected, interpolation = self.project_to_segment(point, start, end)
            segment_distance = dist(point, projected)
            if segment_distance < best_distance:
                best_distance = segment_distance
                best_index = index
                best_interpolation = interpolation

        return best_index, best_interpolation, best_distance

    def map_point(
        self,
        latitude: float,
        longitude: float,
        max_distance_meters: float | None = None,
    ) -> Coordinate:
        point = latlon_to_xy(latitude, longitude, self.lat0, self.lon0)
        segment_index, interpolation, route_distance = self.find_segment(point)
        if max_distance_meters is not None and route_distance > max_distance_meters:
            return latitude, longitude
        ref_start = self.ref_xy[segment_index]
        ref_end = self.ref_xy[segment_index + 1]
        mapped_xy = add(ref_start, scale(sub(ref_end, ref_start), interpolation))
        return xy_to_latlon(mapped_xy[0], mapped_xy[1], self.lat0, self.lon0)
