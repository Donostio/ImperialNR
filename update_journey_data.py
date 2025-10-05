import os
import json
import requests
import time
from datetime import datetime, timedelta

# --- Configuration ---
TFL_APP_ID = os.getenv("TFL_APP_ID", "")
TFL_APP_KEY = os.getenv("TFL_APP_KEY", "")
OUTPUT_FILE = "live_data.json"

# Journey parameters
ORIGIN = "Streatham Common Rail Station"
DESTINATION = "Imperial Wharf Rail Station"
INTERCHANGE_STATION = "Clapham Junction Rail Station"

# TFL API endpoint
TFL_BASE_URL = "https://api.tfl.gov.uk"
NUM_JOURNEYS = 8 # Target the next eight best segments (Direct or One Change)
MIN_TRANSFER_TIME_MINUTES = 1 # Minimum acceptable transfer time
MAX_RETRIES = 3 # Max retries for API calls

# NOTE: Live platform lookups have been removed as the TFL StopPoint API frequently
# returns 404 for these National Rail stations. Platform data will default to "TBC".

# --- Utility Functions ---

def retry_fetch(url, params, max_retries=MAX_RETRIES):
    """Fetches data from a URL with exponential backoff for resilience."""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            print(f"ERROR fetching data ({e}): Attempt {attempt + 1}/{max_retries}. Retrying in {2**attempt}s...")
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
            else:
                raise
        except requests.exceptions.RequestException as e:
            print(f"ERROR connecting to API ({e}): Attempt {attempt + 1}/{max_retries}. Retrying in {2**attempt}s...")
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
            else:
                raise

def get_segment_journeys(origin, destination, departure_time=None):
    """
    Fetch a list of planned journeys for a single segment using the TFL Journey Planner.
    This is used to get all viable train legs for stitching or direct routes.
    The optional departure_time forces the TFL API to search from a specific point.
    """
    url = f"{TFL_BASE_URL}/Journey/JourneyResults/{origin}/to/{destination}"
    
    params = {
        "mode": "overground,national-rail",
        "timeIs": "Departing",
        "journeyPreference": "LeastTime",
        "alternativeRoute": "true"
    }
    
    # If a specific departure time is provided, use it in the API call
    if departure_time:
        # TFL API expects time in HHMM format and date in YYYYMMDD
        params["time"] = departure_time.strftime('%H%M')
        params["date"] = departure_time.strftime('%Y%m%d')
        print(f"DEBUG: Forcing API search for segment from {origin} to start at {departure_time.strftime('%H:%M')}.")
    
    if TFL_APP_ID and TFL_APP_KEY:
        params["app_id"] = TFL_APP_ID
        params["app_key"] = TFL_APP_KEY
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching segment journeys from {origin} to {destination}...")
    try:
        json_data = retry_fetch(url, params)
        return json_data.get('journeys', []) if json_data else []
    except Exception as e:
        print(f"ERROR: Failed to get segment journeys for {origin} to {destination}: {e}")
        return []

def extract_valid_train_legs(journeys, expected_destination):
    """
    Extracts the primary train leg from each journey result that matches the expected
    destination and returns a list of cleaned-up leg objects.
    It filters for unique train services based on time and line ID.
    """
    valid_legs = []
    
    for journey in journeys:
        # A journey result can contain multiple legs (e.g., walk + train), we only care about the first train leg.
        for leg in journey.get('legs', []):
            if leg.get('mode', {}).get('id') in ['overground', 'national-rail']:
                # Basic validation: ensure the arrival point is the expected destination
                if leg.get('arrivalPoint', {}).get('commonName') == expected_destination:
                    valid_legs.append(leg)
                break # Move to the next journey once the first train leg is found
                
    # Use a set comprehension to filter out duplicate legs (multiple journeys might return the same train)
    unique_legs = {
        (leg['departureTime'], leg['arrivalTime'], leg.get('line', {}).get('id')): leg 
        for leg in valid_legs
    }.values()
    
    return list(unique_legs)

def group_connections_by_first_leg(first_legs, second_legs):
    """
    Groups valid second legs (connections) under their corresponding first leg (L1).
    """
    
    grouped_segments = {}
    
    # Sort first legs by departure time for chronological display
    sorted_first_legs = sorted(first_legs, key=lambda l: datetime.fromisoformat(l['departureTime']))

    # --- DEBUGGING: Display available legs for clarity ---
    l1_departures = [datetime.fromisoformat(l['departureTime']).strftime('%H:%M') for l in sorted_first_legs]
    l2_departures = [datetime.fromisoformat(l['departureTime']).strftime('%H:%M') for l in second_legs]
    print(f"DEBUG: L1 (Streatham Common → Clapham Junction) Departures: {', '.join(l1_departures)}")
    print(f"DEBUG: L2 (Clapham Junction → Imperial Wharf) Departures: {', '.join(l2_departures)}")
    # --- END DEBUGGING ---

    for leg1 in sorted_first_legs:
        # Use a unique key for the first leg
        leg1_key = (leg1['departureTime'], leg1['arrivalTime'])
        
        # --- Prepare First Leg Data Structure ---
        if leg1_key not in grouped_segments:
            
            # PLATFORM EXTRACTION LOGIC:
            first_platform = leg1.get('platform', 'TBC')

            dep_time_l1 = datetime.fromisoformat(leg1['departureTime'])
            arr_time_l1 = datetime.fromisoformat(leg1['arrivalTime'])
            
            # Extract scheduled time
            scheduled_dep = leg1.get('scheduledDepartureTime')
            scheduled_dep_str = datetime.fromisoformat(scheduled_dep).strftime('%H:%M') if scheduled_dep else dep_time_l1.strftime('%H:%M')
            
            operator_id = leg1.get('operator', {}).get('id', 'N/A')

            first_leg_data = {
                "origin": leg1['departurePoint']['commonName'],
                "destination": leg1['arrivalPoint']['commonName'],
                "departure": dep_time_l1.strftime('%H:%M'),
                "scheduled_departure": scheduled_dep_str, # NEW FIELD for displaying delay
                "arrival": arr_time_l1.strftime('%H:%M'),
                f"departurePlatform_{leg1['departurePoint']['commonName'].split(' ')[0]}": first_platform,
                "operator": operator_id,
                "status": leg1.get('status', 'On Time'),
                "rawArrivalTime": leg1['arrivalTime']
            }

            grouped_segments[leg1_key] = {
                "type": "One Change", # Label the journey type
                "first_leg": first_leg_data,
                "connections": []
            }
        
        # --- Find and Process Valid Connections (Second Legs) ---
        for leg2 in second_legs:
            arr_time_l1 = datetime.fromisoformat(leg1['arrivalTime'])
            dep_time_l2 = datetime.fromisoformat(leg2['departureTime'])
            
            time_difference = dep_time_l2 - arr_time_l1
            transfer_time_minutes = int(time_difference.total_seconds() / 60)
            
            if transfer_time_minutes >= MIN_TRANSFER_TIME_MINUTES:
                
                # PLATFORM EXTRACTION LOGIC:
                second_platform = leg2.get('platform', 'TBC')

                dep_time_l2 = datetime.fromisoformat(leg2['departureTime'])
                arr_time_l2 = datetime.fromisoformat(leg2['arrivalTime'])
                
                second_leg_data = {
                    "origin": leg2['departurePoint']['commonName'],
                    "destination": leg2['arrivalPoint']['commonName'],
                    "departure": dep_time_l2.strftime('%H:%M'),
                    "arrival": arr_time_l2.strftime('%H:%M'),
                    f"departurePlatform_{leg2['departurePoint']['commonName'].split(' ')[0]}": second_platform,
                    "operator": leg2.get('operator', {}).get('id', 'N/A'),
                    "status": leg2.get('status', 'On Time'),
                    "rawDepartureTime": leg2['departureTime'] # Keep raw time for connection sorting
                }

                # Add the connection
                grouped_segments[leg1_key]['connections'].append({
                    "transferTime": f"{transfer_time_minutes} min",
                    "second_leg": second_leg_data
                })

    # Final Processing and Formatting (for stitched legs)
    final_output = []
    
    # Filter segments to only include those with at least one connection
    segments_with_connections = [s for s in grouped_segments.values() if s['connections']]
    
    # Sort connections for each first leg by the departure time of the second leg
    for segment in segments_with_connections:
        segment['connections'].sort(key=lambda x: datetime.strptime(x['second_leg']['departure'], '%H:%M'))
        
        # Calculate total duration for the stitched journey (approximate)
        l1_dep = datetime.strptime(segment['first_leg']['departure'], '%H:%M')
        l2_arr = datetime.strptime(segment['connections'][0]['second_leg']['arrival'], '%H:%M')
        
        # Handle time crossing midnight for duration calculation
        if l2_arr < l1_dep:
            l2_arr += timedelta(days=1)
        
        total_duration = l2_arr - l1_dep
        segment['totalDuration'] = f"{int(total_duration.total_seconds() / 60)} min"
        segment['arrivalTime'] = segment['connections'][0]['second_leg']['arrival']
        segment['departureTime'] = segment['first_leg']['departure']
        
        # Remove raw times from final output
        segment['first_leg'].pop('rawArrivalTime')
        
        # Create the unique identifier for the segment (used in main for final sorting/filtering)
        segment['unique_id'] = (segment['first_leg']['departure'], segment['first_leg']['operator'])

        for conn in segment['connections']:
            conn['second_leg'].pop('rawDepartureTime')
            
        final_output.append(segment)
        
        # Log the result for the console output
        conn_times = [c['second_leg']['departure'] for c in segment['connections']]
        print(f"✓ Stitched Segment ({segment['first_leg']['departure']} → {segment['first_leg']['arrival']}): Found {len(conn_times)} connections ({', '.join(conn_times)})")


    return final_output

def process_direct_journey(journey, leg):
    """Processes a single leg (direct journey) into the final segment format."""
    
    # PLATFORM EXTRACTION LOGIC:
    first_platform = leg.get('platform', 'TBC')

    dep_time = datetime.fromisoformat(leg['departureTime'])
    arr_time = datetime.fromisoformat(leg['arrivalTime'])
    
    # Extract scheduled time
    scheduled_dep = leg.get('scheduledDepartureTime')
    scheduled_dep_str = datetime.fromisoformat(scheduled_dep).strftime('%H:%M') if scheduled_dep else dep_time.strftime('%H:%M')
    
    # Calculate total duration from TFL journey object
    total_duration = journey.get('duration', 'N/A')
    
    operator_id = leg.get('operator', {}).get('id', 'N/A')
    # Unique identifier for the direct train
    unique_id = (dep_time.strftime('%H:%M'), operator_id)

    return {
        "type": "Direct", 
        "departureTime": dep_time.strftime('%H:%M'),
        "arrivalTime": arr_time.strftime('%H:%M'),
        "totalDuration": f"{total_duration} min" if isinstance(total_duration, int) else total_duration,
        "status": leg.get('status', 'On Time'),
        "unique_id": unique_id, # Add unique identifier for cross-referencing
        # Note: segment_id and live_updated_at are added in main()
        "first_leg": {
            "origin": leg['departurePoint']['commonName'],
            "destination": leg['arrivalPoint']['commonName'],
            "departure": dep_time.strftime('%H:%M'),
            "scheduled_departure": scheduled_dep_str,
            "arrival": arr_time.strftime('%H:%M'),
            f"departurePlatform_{leg['departurePoint']['commonName'].split(' ')[0]}": first_platform,
            "operator": operator_id,
            "status": leg.get('status', 'On Time'),
        },
        "connections": [] # Direct trains have no connections
    }


def get_direct_journeys():
    """Fetches and processes direct journeys from ORIGIN to DESTINATION."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Fetching direct journeys from {ORIGIN} to {DESTINATION}...")
    
    # Get all journeys from Streatham Common to Imperial Wharf
    journeys = get_segment_journeys(ORIGIN, DESTINATION)
    direct_journeys = []

    for journey in journeys:
        # Check for direct train: Journey has exactly one leg, and that leg is a train.
        if len(journey.get('legs', [])) == 1:
            leg = journey['legs'][0]
            if leg.get('mode', {}).get('id') in ['overground', 'national-rail']:
                
                # Check that the leg destination is the final destination (Imperial Wharf)
                if leg.get('arrivalPoint', {}).get('commonName') == DESTINATION:
                    
                    # Process and format the direct journey
                    processed_journey = process_direct_journey(journey, leg)
                    direct_journeys.append(processed_journey)
                    
                    print(f"✓ Found direct journey: {processed_journey['departureTime']} → {processed_journey['arrivalTime']}")
                    
    return direct_journeys


def get_one_change_journeys(direct_journeys):
    """
    Fetches all train legs for the two segments, manually groups them, and filters
    out any first legs that correspond to a direct journey.
    """
    
    # 1. Fetch all unique train legs from Streatham Common to Clapham Junction (searches from now)
    journeys_l1 = get_segment_journeys(ORIGIN, INTERCHANGE_STATION)
    first_legs = extract_valid_train_legs(journeys_l1, INTERCHANGE_STATION)
    print(f"DEBUG: Found {len(first_legs)} unique legs for the first segment.")
    
    if not first_legs:
        print("ERROR: Could not retrieve any first train legs.")
        return []

    # --- NEW FILTERING LOGIC ---
    # Create a set of unique identifiers (departure time, operator ID) for all found direct trains.
    # This identifies the services that MUST NOT be used as Leg 1 in a stitched journey.
    direct_train_ids = {
        j['unique_id'] 
        for j in direct_journeys
    }
    
    # Filter out any Leg 1 that matches a direct train service
    filtered_first_legs = []
    
    for leg in first_legs:
        # TFL returns the full train journey's operator even when segmenting a leg
        leg_id = (datetime.fromisoformat(leg['departureTime']).strftime('%H:%M'), leg.get('operator', {}).get('id'))
        
        if leg_id in direct_train_ids:
            print(f"DEBUG: Filtering Leg 1 {leg_id[0]} as it is a known direct service.")
            continue
            
        filtered_first_legs.append(leg)

    first_legs = filtered_first_legs
    print(f"DEBUG: Filtered down to {len(first_legs)} unique Leg 1s (non-direct).")
    # --- END NEW FILTERING LOGIC ---
    
    # 2. Fetch all unique train legs from Clapham Junction to Imperial Wharf
    # Look 90 minutes into the future to ensure we capture a good range of connecting trains.
    future_time = datetime.now() + timedelta(minutes=90)
    journeys_l2 = get_segment_journeys(INTERCHANGE_STATION, DESTINATION, departure_time=future_time)
    second_legs = extract_valid_train_legs(journeys_l2, DESTINATION)
    print(f"DEBUG: Found {len(second_legs)} unique legs for the second segment.")
    
    if not second_legs:
        print("ERROR: Could not retrieve sufficient train legs for stitching.")
        return []

    # 3. Group and process the connections
    processed_segments = group_connections_by_first_leg(first_legs, second_legs)

    if not processed_segments:
        print(f"No valid segments found with connections meeting the minimum {MIN_TRANSFER_TIME_MINUTES}-minute transfer.")
        return []

    return processed_segments

def main():
    # 1. Get Direct Journeys
    direct_data = get_direct_journeys()
    
    # 2. Get One-Change Journeys (Stitched), filtering out the direct trains
    # The get_one_change_journeys function now handles filtering based on direct_data
    stitched_data = get_one_change_journeys(direct_data) 
    
    # 3. Combine and Sort all results
    combined_data = direct_data + stitched_data
    
    # Sort the list by the departure time of the first leg (or direct journey)
    sorted_data = sorted(combined_data, key=lambda x: datetime.strptime(x['first_leg']['departure'], '%H:%M'))
    
    # 4. Add IDs, Timestamps, and slice the final list
    final_output = []
    current_time = datetime.now().strftime('%H:%M:%S')

    for idx, segment in enumerate(sorted_data):
        # Assign unified metadata
        segment['segment_id'] = idx + 1
        segment['live_updated_at'] = current_time
        
        # Remove the temporary unique_id field before final output
        if 'unique_id' in segment:
            segment.pop('unique_id')
        
        final_output.append(segment)

    # Limit to NUM_JOURNEYS segments (the best N options overall)
    final_output = final_output[:NUM_JOURNEYS]
    
    if final_output:
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(final_output, f, indent=4)
        print(f"\n✓ Successfully saved {len(final_output)} journey segments (Direct and One Change) to {OUTPUT_FILE}")
    else:
        print(f"\n⚠ Failed to retrieve or process any valid journey data. {OUTPUT_FILE} remains unchanged.")


if __name__ == "__main__":
    main()
