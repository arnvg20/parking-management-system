from __future__ import annotations

import math

from .models import BBox, Point2D


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def euclidean_distance(left: Point2D, right: Point2D) -> float:
    return math.hypot(left.x - right.x, left.y - right.y)


def polygon_centroid(polygon: tuple[Point2D, ...]) -> Point2D:
    if not polygon:
        return Point2D(0.0, 0.0)
    return Point2D(
        x=sum(point.x for point in polygon) / len(polygon),
        y=sum(point.y for point in polygon) / len(polygon),
    )


def polygon_bounds(polygon: tuple[Point2D, ...]) -> BBox:
    x_values = [point.x for point in polygon]
    y_values = [point.y for point in polygon]
    return BBox(
        x1=min(x_values),
        y1=min(y_values),
        x2=max(x_values),
        y2=max(y_values),
    )


def polygon_diagonal_length(polygon: tuple[Point2D, ...]) -> float:
    bounds = polygon_bounds(polygon)
    return max(1.0, math.hypot(bounds.width, bounds.height))


def point_in_polygon(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    inside = False
    total = len(polygon)
    if total < 3:
        return False

    previous = polygon[-1]
    for current in polygon:
        intersects = ((current.y > point.y) != (previous.y > point.y)) and (
            point.x < (previous.x - current.x) * (point.y - current.y) / ((previous.y - current.y) or 1e-9) + current.x
        )
        if intersects:
            inside = not inside
        previous = current
    return inside


def sampled_overlap_ratio(rect: BBox, polygon: tuple[Point2D, ...], samples_x: int = 6, samples_y: int = 4) -> float:
    if rect.width <= 0 or rect.height <= 0 or samples_x <= 0 or samples_y <= 0:
        return 0.0

    hit_count = 0
    sample_count = samples_x * samples_y
    step_x = rect.width / samples_x
    step_y = rect.height / samples_y

    for row in range(samples_y):
        for col in range(samples_x):
            point = Point2D(
                x=rect.x1 + (step_x * (col + 0.5)),
                y=rect.y1 + (step_y * (row + 0.5)),
            )
            if point_in_polygon(point, polygon):
                hit_count += 1

    return hit_count / sample_count


def normalized_distance_score(distance_px: float, normalizer_px: float) -> float:
    if normalizer_px <= 0:
        return 0.0
    return clamp(1.0 - (distance_px / normalizer_px))


def normalized_axis_distance(point: Point2D, bbox: BBox) -> tuple[float, float]:
    if bbox.width <= 0 or bbox.height <= 0:
        return (1.0, 1.0)

    offset_x = abs(point.x - bbox.center.x) / max(1.0, bbox.width / 2.0)
    offset_y = abs(point.y - bbox.center.y) / max(1.0, bbox.height / 2.0)
    return (offset_x, offset_y)
