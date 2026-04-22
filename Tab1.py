import math

SECTION_LAYOUTS = {
    'A': {
        'name': 'Section A',
        'spaces': 10,
        'corners': {
            'top_left': (43.77139444, -79.50583056),
            'top_right': (43.77140833, -79.50577222),
            'bottom_left': (43.77108611, -79.50569722),
            'bottom_right': (43.77110000, -79.50563889),
        },
    },
    'B': {
        'name': 'Section B',
        'spaces': 10,
        'corners': {
            'top_left': (43.77150278, -79.50571111),
            'top_right': (43.77151667, -79.50565000),
            'bottom_left': (43.77118889, -79.50557778),
            'bottom_right': (43.77120000, -79.50551111),
        },
    },
    'C': {
        'name': 'Section C',
        'spaces': 10,
        'corners': {
            'top_left': (43.77090278, -79.50561944),
            'top_right': (43.77091667, -79.50555833),
            'bottom_left': (43.77052222, -79.50545833),
            'bottom_right': (43.77053889, -79.50539167),
        },
    },
    'D': {
        'name': 'Section D',
        'spaces': 10,
        'corners': {
            'top_left': (43.77084167, -79.50543889),
            'top_right': (43.77085556, -79.50537778),
            'bottom_left': (43.77059722, -79.50533611),
            'bottom_right': (43.77061111, -79.50527222),
        },
    },
}

LOT_BOUNDS = {
    'top_left': (43.77146111, -79.50618333),
    'top_right': (43.77158889, -79.50555833),
    'bottom_left': (43.77033056, -79.50585556),
    'bottom_right': (43.77052222, -79.50512778),
}

ENVIRONMENTAL_DETECTIONS = {
    'cracks': [
        {
            'label': 'Crack 1',
            'latitude': 43.77138056,
            'longitude': -79.50593333,
        },
        {
            'label': 'Crack 2',
            'latitude': 43.77146944,
            'longitude': -79.50576944,
        },
        {
            'label': 'Crack 3',
            'latitude': 43.77118611,
            'longitude': -79.50586667,
        },
    ],
    'signs': [
        {
            'label': 'Wrong Way / Do Not Enter Sign 1',
            'kind': 'Wrong Way / Do Not Enter',
            'latitude': 43.77050278,
            'longitude': -79.50550000,
        },
        {
            'label': 'Wrong Way / Do Not Enter Sign 2',
            'kind': 'Wrong Way / Do Not Enter',
            'latitude': 43.77097222,
            'longitude': -79.50568056,
        },
    ],
}


def ordered_corners(corner_set):
    """Return corners in clockwise order for polygon drawing."""
    return [
        corner_set['top_left'],
        corner_set['top_right'],
        corner_set['bottom_right'],
        corner_set['bottom_left'],
    ]


def interpolate_point(start_point, end_point, ratio):
    """Linearly interpolate between two latitude/longitude points."""
    return (
        start_point[0] + (end_point[0] - start_point[0]) * ratio,
        start_point[1] + (end_point[1] - start_point[1]) * ratio,
    )


def polygon_center(corners):
    """Get the center point of a polygon from its corner coordinates."""
    return (
        sum(point[0] for point in corners) / len(corners),
        sum(point[1] for point in corners) / len(corners),
    )


def generate_section_spaces(section_id, section_data):
    """Split a section polygon into evenly sized parking stall polygons."""
    corners = section_data['corners']
    top_left = corners['top_left']
    top_right = corners['top_right']
    bottom_left = corners['bottom_left']
    bottom_right = corners['bottom_right']

    space_locations = {}
    total_spaces = section_data['spaces']

    for index in range(total_spaces):
        start_ratio = index / total_spaces
        end_ratio = (index + 1) / total_spaces

        left_top = interpolate_point(top_left, bottom_left, start_ratio)
        right_top = interpolate_point(top_right, bottom_right, start_ratio)
        right_bottom = interpolate_point(top_right, bottom_right, end_ratio)
        left_bottom = interpolate_point(top_left, bottom_left, end_ratio)

        polygon = [left_top, right_top, right_bottom, left_bottom]
        center_lat, center_lon = polygon_center(polygon)
        space_id = f'{section_id}{index + 1}'

        space_locations[space_id] = {
            'section_id': section_id,
            'polygon': polygon,
            'latitude': center_lat,
            'longitude': center_lon,
            'occupied': False,
            'vehicle_data': None,
        }

    return space_locations


def build_parking_layout():
    """Create section metadata and generated space geometry."""
    sections = {}
    spaces = {}

    for section_id, section_data in SECTION_LAYOUTS.items():
        polygon = ordered_corners(section_data['corners'])
        center_lat, center_lon = polygon_center(polygon)

        sections[section_id] = {
            'name': section_data['name'],
            'spaces': section_data['spaces'],
            'corners': polygon,
            'center': {
                'latitude': center_lat,
                'longitude': center_lon,
            },
        }

        spaces.update(generate_section_spaces(section_id, section_data))

    return sections, spaces


parking_sections, parking_spaces = build_parking_layout()
lot_bounds = ordered_corners(LOT_BOUNDS)
environmental_detections = ENVIRONMENTAL_DETECTIONS


def distance_between_points(lat1, lon1, lat2, lon2):
    """Calculate distance in meters between two coordinates."""
    radius_meters = 6371000

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a_value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c_value = 2 * math.asin(math.sqrt(a_value))

    return radius_meters * c_value


def find_matching_space(vehicle_lat, vehicle_lon, offset_meters=1):
    """Find parking space that matches vehicle location within offset."""
    for space_id, space_data in parking_spaces.items():
        distance = distance_between_points(
            vehicle_lat,
            vehicle_lon,
            space_data['latitude'],
            space_data['longitude'],
        )

        if distance <= offset_meters:
            return space_id

    return None


def get_sample_vehicles_from_spaces():
    """Generate sample vehicles from actual parking space center points."""
    vehicles = []
    ordered_space_ids = sorted(parking_spaces.keys(), key=lambda value: (value[0], int(value[1:])))

    for index, space_id in enumerate(ordered_space_ids[:18]):
        space = parking_spaces[space_id]
        vehicles.append({
            'latitude': space['latitude'],
            'longitude': space['longitude'],
            'license_plate': f'VEH{index + 1:03d}',
        })

    return vehicles


SAMPLE_VEHICLES = get_sample_vehicles_from_spaces()


def load_sample_vehicles():
    """Load sample vehicles into parking spaces by matching locations."""
    from datetime import datetime

    count = 0
    for vehicle in SAMPLE_VEHICLES:
        matching_space = find_matching_space(
            vehicle['latitude'],
            vehicle['longitude'],
            offset_meters=1,
        )
        if matching_space:
            parking_spaces[matching_space]['occupied'] = True
            parking_spaces[matching_space]['vehicle_data'] = {
                'license_plate': vehicle['license_plate'],
                'time': datetime.now().isoformat(),
                'latitude': vehicle['latitude'],
                'longitude': vehicle['longitude'],
            }
            count += 1

