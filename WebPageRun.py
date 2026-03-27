from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
from Tab1 import parking_spaces, find_matching_space, load_sample_vehicles

app = Flask(__name__)
CORS(app)

# Load sample vehicles on startup
load_sample_vehicles()

# Frontend route
@app.route('/')
def index():
    with open('Website.html', 'r') as f:
        return f.read()

# API routes
@app.route('/api/parking-spaces', methods=['GET'])
def get_parking_spaces():
    """Get all parking space data"""
    result = {}
    for space_id, data in parking_spaces.items():
        result[space_id] = {
            'latitude': data['latitude'],
            'longitude': data['longitude'],
            'occupied': data['occupied'],
            'vehicle_data': data['vehicle_data']
        }
    return jsonify(result)

@app.route('/api/add-vehicle', methods=['POST'])
def add_vehicle():
    """Add vehicle data and update parking space status"""
    data = request.json
    
    vehicle_lat = data.get('latitude')
    vehicle_lon = data.get('longitude')
    license_plate = data.get('license_plate')
    
    if vehicle_lat is None or vehicle_lon is None:
        return jsonify({'error': 'Missing latitude or longitude'}), 400
    
    matching_space = find_matching_space(vehicle_lat, vehicle_lon, offset_meters=1)
    
    if matching_space:
        parking_spaces[matching_space]['occupied'] = True
        parking_spaces[matching_space]['vehicle_data'] = {
            'license_plate': license_plate,
            'time': datetime.now().isoformat(),
            'latitude': vehicle_lat,
            'longitude': vehicle_lon
        }
        
        return jsonify({
            'status': 'success',
            'message': f'Vehicle parked in space {matching_space}',
            'parking_space': matching_space
        })
    else:
        return jsonify({
            'status': 'error',
            'message': 'No matching parking space found'
        }), 404

@app.route('/api/remove-vehicle', methods=['POST'])
def remove_vehicle():
    """Remove vehicle from parking space"""
    data = request.json
    space_id = data.get('space_id')
    
    if space_id not in parking_spaces:
        return jsonify({'error': 'Invalid space ID'}), 400
    
    parking_spaces[space_id]['occupied'] = False
    parking_spaces[space_id]['vehicle_data'] = None
    
    return jsonify({
        'status': 'success',
        'message': f'Vehicle removed from space {space_id}'
    })

@app.route('/api/toggle-space', methods=['POST'])
def toggle_space():
    """Toggle parking space status manually"""
    data = request.json
    space_id = data.get('space_id')
    
    if space_id not in parking_spaces:
        return jsonify({'error': 'Invalid space ID'}), 400
    
    parking_spaces[space_id]['occupied'] = not parking_spaces[space_id]['occupied']
    
    return jsonify({
        'status': 'success',
        'occupied': parking_spaces[space_id]['occupied']
    })

@app.route('/api/space-locations', methods=['GET'])
def get_space_locations():
    """Get location points for all parking spaces"""
    locations = {}
    for space_id, data in parking_spaces.items():
        locations[space_id] = {
            'latitude': data['latitude'],
            'longitude': data['longitude']
        }
    return jsonify(locations)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)