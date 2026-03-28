import os
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
from datetime import datetime
from Tab1 import (
    find_matching_space,
    load_sample_vehicles,
    lot_bounds,
    parking_sections,
    parking_spaces,
)

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).resolve().parent
JETSON_API_BASE_URL = 'http://10.24.21.85:5050/api/system'
VIDEO_FEED_CONFIG_PATH = BASE_DIR / 'video_feed_config.json'
ALLOWED_VIDEO_FEED_MODES = {'mjpeg', 'video', 'iframe'}


def load_video_feed_config():
    """Load persisted video feed settings from disk."""
    if VIDEO_FEED_CONFIG_PATH.exists():
        try:
            with VIDEO_FEED_CONFIG_PATH.open('r', encoding='utf-8') as file_handle:
                stored_config = json.load(file_handle)
        except (OSError, json.JSONDecodeError):
            stored_config = {}
    else:
        stored_config = {}

    url = str(stored_config.get('url', '')).strip()
    mode = str(stored_config.get('mode', 'mjpeg')).strip().lower()
    if mode not in ALLOWED_VIDEO_FEED_MODES:
        mode = 'mjpeg'

    return {
        'url': url,
        'mode': mode,
    }


def save_video_feed_config(config):
    """Persist video feed settings to disk."""
    with VIDEO_FEED_CONFIG_PATH.open('w', encoding='utf-8') as file_handle:
        json.dump(config, file_handle, indent=2)


video_feed_config = load_video_feed_config()

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
            'section_id': data['section_id'],
            'latitude': data['latitude'],
            'longitude': data['longitude'],
            'polygon': [
                {'latitude': point[0], 'longitude': point[1]}
                for point in data['polygon']
            ],
            'occupied': data['occupied'],
            'vehicle_data': data['vehicle_data']
        }
    return jsonify(result)


@app.route('/api/map-data', methods=['GET'])
def get_map_data():
    """Get map bounds, section geometry, and parking space data."""
    sections = {}
    for section_id, data in parking_sections.items():
        sections[section_id] = {
            'name': data['name'],
            'spaces': data['spaces'],
            'center': data['center'],
            'corners': [
                {'latitude': point[0], 'longitude': point[1]}
                for point in data['corners']
            ],
        }

    spaces = {}
    for space_id, data in parking_spaces.items():
        spaces[space_id] = {
            'section_id': data['section_id'],
            'latitude': data['latitude'],
            'longitude': data['longitude'],
            'polygon': [
                {'latitude': point[0], 'longitude': point[1]}
                for point in data['polygon']
            ],
            'occupied': data['occupied'],
            'vehicle_data': data['vehicle_data'],
        }

    return jsonify({
        'lot_bounds': [
            {'latitude': point[0], 'longitude': point[1]}
            for point in lot_bounds
        ],
        'sections': sections,
        'spaces': spaces,
    })


@app.route('/api/video-feed/config', methods=['GET'])
def get_video_feed_config():
    """Return the configured live video feed settings."""
    return jsonify(video_feed_config)


@app.route('/api/video-feed/config', methods=['POST'])
def update_video_feed_config():
    """Store the live video feed URL and how the frontend should render it."""
    global video_feed_config

    data = request.get_json(silent=True) or {}
    url = str(data.get('url', '')).strip()
    mode = str(data.get('mode', 'mjpeg')).strip().lower()

    if url:
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {'http', 'https'} or not parsed_url.netloc:
            return jsonify({'error': 'Feed URL must be a valid http or https URL'}), 400

    if mode not in ALLOWED_VIDEO_FEED_MODES:
        return jsonify({
            'error': f'Unsupported video mode. Use one of: {", ".join(sorted(ALLOWED_VIDEO_FEED_MODES))}'
        }), 400

    video_feed_config = {
        'url': url,
        'mode': mode,
    }
    save_video_feed_config(video_feed_config)

    return jsonify({
        'status': 'success',
        'feed': video_feed_config,
    })


@app.route('/api/video-feed/proxy', methods=['GET'])
def proxy_video_feed():
    """Proxy the configured live feed so the frontend can load it from the app origin."""
    feed_url = video_feed_config.get('url', '').strip()
    mode = video_feed_config.get('mode', 'mjpeg')

    if not feed_url:
        return jsonify({'error': 'No live video feed has been configured yet'}), 404

    if mode == 'iframe':
        return jsonify({'error': 'Iframe feeds should be loaded directly from their configured URL'}), 400

    request_obj = Request(
        feed_url,
        headers={'User-Agent': 'ParkingManagementSystem/1.0'},
    )

    try:
        upstream_response = urlopen(request_obj, timeout=10)
    except HTTPError as error:
        error_body = error.read().decode('utf-8', errors='replace')
        return jsonify({
            'error': f'Video source returned HTTP {error.code}',
            'details': error_body,
        }), error.code
    except URLError as error:
        return jsonify({
            'error': f'Unable to reach video source at {feed_url}',
            'details': str(error.reason),
        }), 502

    content_type = upstream_response.headers.get('Content-Type', 'application/octet-stream')

    def generate():
        try:
            while True:
                chunk = upstream_response.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream_response.close()

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers={'Cache-Control': 'no-store'},
    )

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


def forward_jetson_command(command):
    """Forward Jetson power commands to the local control service."""
    target_url = f'{JETSON_API_BASE_URL}/{command}'
    request_obj = Request(target_url, method='POST')

    try:
        with urlopen(request_obj, timeout=5) as response:
            raw_body = response.read().decode('utf-8', errors='replace')
            payload = {'raw_response': raw_body} if raw_body else {}

            if raw_body and 'application/json' in response.headers.get('Content-Type', ''):
                try:
                    payload = json.loads(raw_body)
                except json.JSONDecodeError:
                    payload = {'raw_response': raw_body}

            return jsonify({
                'status': 'success',
                'command': command,
                'jetson_response': payload
            }), response.status
    except HTTPError as error:
        error_body = error.read().decode('utf-8', errors='replace')
        return jsonify({
            'status': 'error',
            'command': command,
            'message': f'Jetson control service returned HTTP {error.code}',
            'details': error_body
        }), error.code
    except URLError as error:
        return jsonify({
            'status': 'error',
            'command': command,
            'message': f'Unable to reach Jetson control service at {target_url}',
            'details': str(error.reason)
        }), 502


@app.route('/api/system/on', methods=['POST'])
def start_jetson():
    return forward_jetson_command('on')


@app.route('/api/system/off', methods=['POST'])
def stop_jetson():
    return forward_jetson_command('off')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5001'))
    app.run(debug=True, host='0.0.0.0', port=port)
