import math
import random

SECTION1_CORNERS = [
    (43.771507, -79.505683),
    (43.771095, -79.505462),
    (43.771068, -79.505729),
    (43.771396, -79.505836)
]

def get_bounding_box(corners):
    """Get north, south, east, west from corners"""
    lats = [c[0] for c in corners]
    lons = [c[1] for c in corners]
    return {
        'north': max(lats),
        'south': min(lats),
        'east': max(lons),
        'west': min(lons)
    }

def generate_parking_space_locations():
    """Generate random location points for each parking space within bounds"""
    bbox = get_bounding_box(SECTION1_CORNERS)
    
    space_locations = {}
    lots = ['A', 'B', 'C', 'D']
    
    # Seed for reproducibility
    random.seed(42)
    
    lat_range = bbox['north'] - bbox['south']
    lon_range = bbox['east'] - bbox['west']
    
    lot_index = 0
    for lot_letter in lots:
        # Divide the bounding box into 4 quadrants for each lot
        lot_row = lot_index // 2
        lot_col = lot_index % 2
        
        lot_north = bbox['north'] - (lot_row * lat_range / 2)
        lot_south = bbox['north'] - ((lot_row + 1) * lat_range / 2)
        lot_east = bbox['west'] + ((lot_col + 1) * lon_range / 2)
        lot_west = bbox['west'] + (lot_col * lon_range / 2)
        
        # Generate 10 spaces within each lot's area
        for space_num in range(1, 11):
            space_id = f"{lot_letter}{space_num}"
            
            # Generate random location within this lot's quadrant
            random_lat = random.uniform(lot_south, lot_north)
            random_lon = random.uniform(lot_west, lot_east)
            
            space_locations[space_id] = {
                'latitude': random_lat,
                'longitude': random_lon,
                'occupied': False,
                'vehicle_data': None
            }
        
        lot_index += 1
    
    return space_locations

parking_spaces = generate_parking_space_locations()

def distance_between_points(lat1, lon1, lat2, lon2):
    """Calculate distance in meters between two coordinates"""
    R = 6371000
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    
    return R * c

def find_matching_space(vehicle_lat, vehicle_lon, offset_meters=1):
    """Find parking space that matches vehicle location within offset"""
    for space_id, space_data in parking_spaces.items():
        distance = distance_between_points(
            vehicle_lat, vehicle_lon,
            space_data['latitude'], space_data['longitude']
        )
        
        if distance <= offset_meters:
            return space_id
    
    return None

# Sample vehicle data (18 vehicles to occupy 18 spaces)
def get_sample_vehicles_from_spaces():
    """Generate sample vehicles from actual parking space locations"""
    vehicles = []
    space_ids = sorted(parking_spaces.keys())
    
    # Get first 18 spaces and use their exact locations
    for i, space_id in enumerate(space_ids[:18]):
        space = parking_spaces[space_id]
        vehicles.append({
            'latitude': space['latitude'],
            'longitude': space['longitude'],
            'license_plate': f'VEH{i+1:03d}'
        })
    
    return vehicles

SAMPLE_VEHICLES = get_sample_vehicles_from_spaces()

def load_sample_vehicles():
    """Load sample vehicles into parking spaces by matching locations"""
    from datetime import datetime
    count = 0
    for vehicle in SAMPLE_VEHICLES:
        matching_space = find_matching_space(vehicle['latitude'], vehicle['longitude'], offset_meters=1)
        if matching_space:
            parking_spaces[matching_space]['occupied'] = True
            parking_spaces[matching_space]['vehicle_data'] = {
                'license_plate': vehicle['license_plate'],
                'time': datetime.now().isoformat(),
                'latitude': vehicle['latitude'],
                'longitude': vehicle['longitude']
            }
            count += 1
            print(f"✓ Vehicle {vehicle['license_plate']} matched to space {matching_space}")
        else:
            print(f"✗ Vehicle {vehicle['license_plate']} - No matching space found")
    print(f"\n✓ Total vehicles loaded: {count}/18")