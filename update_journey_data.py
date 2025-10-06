import os
import json
import logging
from datetime import datetime
from requests import Session, HTTPError, ConnectionError, Timeout

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
# ---------------------

# --- Configuration ---
RTT_USERNAME = os.getenv("RTT_USERNAME") # Loaded from GitHub Secret
RTT_PASSWORD = os.getenv("RTT_PASSWORD") # Loaded from GitHub Secret
OUTPUT_FILE = "live_data.json"
API_BASE_URL = "https://api.rtt.io/api/v1"

# ✅ FIX: Use the actual stations from the user's journey
# Streatham Common (Origin)
ORIGIN_STATION_CRS = "SRC" 
# Clapham Junction (Connection)
CONNECTION_STATION_CRS = "CLJ" 
# Imperial Wharf (Final Destination - used for filtering services)
FINAL_DESTINATION_CRS = "IMW" 
# ---------------------

# --- RTT Client ---
class RttClient:
    """Client for the Realtime Trains JSON API."""
    
    def __init__(self, username, password):
        if not username or not password:
             raise ValueError("RTT_USERNAME and RTT_PASSWORD must be provided.")
        
        self.session = Session()
        # Basic HTTP Auth is required
        self.session.auth = (username, password)
        self.base_url = API_BASE_URL

    def _make_request(self, endpoint):
        """Internal method to handle API requests."""
        url = self.base_url + endpoint
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)
            return response.json()
        except HTTPError as e:
            logging.error(f"HTTP Error {e.response.status_code} fetching {endpoint}: {e}")
            return None
        except (ConnectionError, Timeout) as e:
            logging.error(f"Connection/Timeout Error fetching {endpoint}: {e}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred fetching {endpoint}: {e}")
            return None

    def get_station_departures(self, station_crs):
        """
        Fetches live departures for a given station.
        Example endpoint: /json/search/<station>
        """
        endpoint = f"/json/search/{station_crs}"
        logging.info(f"Fetching departures for {station_crs} from RTT API...")
        return self._make_request(endpoint)

    def get_service_details(self, service_uid, date_of_service):
        """
        Fetches detailed information for a specific service.
        Example endpoint: /json/service/<serviceUid>/<date>
        """
        # Date must be in YYYY/MM/DD format
        date_str = date_of_service.strftime("%Y/%m/%d")
        endpoint = f"/json/service/{service_uid}/{date_str}"
        logging.info(f"Fetching details for service {service_uid}...")
        return self._make_request(endpoint)

def process_rtt_data(live_data, connection_crs, final_destination_crs, rtt_client):
    """Processes RTT data to extract the two-leg journey information."""
    if not live_data or 'services' not in live_data:
        return []

    processed_journeys = []
    current_time_str = datetime.now().strftime("%H:%M:%S")

    # 1. Identify First Leg: ORIGIN (SRC) to CONNECTION (CLJ)
    # RTT API response structure is different from the original JSON, so we reconstruct it.
    for service in live_data['services']:
        # RTT API can sometimes return services that don't call at the queried station, 
        # so check if the station is in the subsequent schedule.
        if 'locationDetail' not in service:
             continue
        
        # Check if the service calls at the Connection Station (CLJ)
        # We assume any service that calls at CLJ can be a potential first leg.
        # This is a simplification; a full solution would check the calling points.
        
        # Check if the service stops at the connection station. The response is a single object 
        # for the queried station's details in the schedule.
        departure_time = service['locationDetail'].get('realtimeDeparture', service['locationDetail'].get('gbttBookedDeparture'))
        if not departure_time:
             continue # Skip if no departure time is available

        # RTT times are HHMM, we want HH:MM
        departure_time_formatted = f"{departure_time[:2]}:{departure_time[2:]}"
        
        # Get the final destination of this service (not necessarily IMW)
        service_destination_crs = service.get('destination', [{}])[0].get('crs', 'N/A')
        
        # Determine the status and platform
        platform = service['locationDetail'].get('platform', 'TBC')
        is_delayed = service['locationDetail'].get('isDelayed', False)
        status = "Delayed" if is_delayed else "On Time"

        # Check for services that *terminate* at the connection point (CLJ) or go beyond.
        # For a two-leg journey, the first train often terminates at the connection (CLJ).
        # We'll just look for a transfer at the connection point.
        
        first_leg = {
            "origin": live_data.get('locationDetail', {}).get('description', 'Streatham Common Rail Station'),
            "destination": service_destination_crs, # The final destination of this specific service
            "departure": departure_time_formatted,
            "scheduled_departure": departure_time_formatted, # RTT gives us the actual time, use it for both
            "arrival": "TBC", # RTT Search endpoint doesn't give arrival at CLJ directly in the board.
            "departurePlatform_Streatham": platform,
            "operator": service.get('operator', 'N/A'),
            "status": status,
            "serviceUid": service.get('serviceUid', None),
            "dateOfService": service.get('runDate', None)
        }

        # 2. Find Second Leg: CONNECTION (CLJ) to FINAL_DESTINATION (IMW)
        # This requires a second API call, which can be inefficient in a scheduled script.
        # For simplicity and to match the original structure, we will create a *placeholder* # for the connection, as RTT does not provide journey planning.
        # A more robust solution would require fetching the CLJ departure board and matching 
        # transfer times, but for a single scheduled run, this is better.

        # Estimate a 5 minute transfer time for a placeholder connection
        # This placeholder replaces the old TFL-based second leg logic.
        placeholder_connection = {
            "transferTime": "5 min (Assumed)",
            "second_leg": {
                "origin": "Clapham Junction Rail Station",
                "destination": "Imperial Wharf Rail Station",
                "departure": "TBC (Need CLJ Board)",
                "arrival": "TBC (Need CLJ Board)",
                "departurePlatform_Clapham": "TBC (Need CLJ Board)",
                "operator": "N/A",
                "status": "Live Data Pending"
            }
        }

        # Structure the final journey object to match the user's original live_data.json
        processed_journeys.append({
            "type": "Live RTT Update",
            "first_leg": first_leg,
            "connections": [placeholder_connection],
            "totalDuration": "Live Update",
            "arrivalTime": "Live Update",
            "departureTime": departure_time_formatted,
            "segment_id": len(processed_journeys) + 1,
            "live_updated_at": current_time_str
        })
        
        # Limiting to first 5 services for brevity and to avoid excessive processing
        if len(processed_journeys) >= 5:
            break

    # Save the raw RTT data as well for debugging if needed
    processed_journeys.insert(0, {"meta_data": {"rtt_status": "Partial Data", "note": "RTT is a departure board, not a journey planner. Second leg from CLJ to IMW is placeholder. First leg is from SRC to the service's final destination."}})

    return processed_journeys

def main():
    """Main execution function to fetch, process, and save data."""
    
    if not RTT_USERNAME or not RTT_PASSWORD:
        logging.error("RTT_USERNAME or RTT_PASSWORD is not set. Exiting.")
        print("\n❌ FAILED TO INITIALIZE: RTT credentials must be provided via environment variables or secrets.")
        return 
        
    try:
        client = RttClient(RTT_USERNAME, RTT_PASSWORD)
    except ValueError as e:
        print(f"\n❌ FAILED TO INITIALIZE RTT Client: {e}")
        return 
        
    # 1. Get Live Departures for the Origin Station
    live_data_raw = client.get_station_departures(ORIGIN_STATION_CRS)

    # 2. Process and Save Data
    if live_data_raw:
        processed_data = process_rtt_data(live_data_raw, CONNECTION_STATION_CRS, FINAL_DESTINATION_CRS, client)
        
        try:
            with open(OUTPUT_FILE, 'w') as f:
                json.dump(processed_data, f, indent=4)
            logging.info(f"Data saved successfully to {OUTPUT_FILE}")
            print(f"\n✅ SUCCESSFULLY fetched live departures for {ORIGIN_STATION_CRS} and updated {OUTPUT_FILE}")
            print(f"   Saved {len(processed_data) - 1} services.")
        except IOError as e:
            logging.error(f"Failed to write to file {OUTPUT_FILE}: {e}")
    else:
        logging.warning(f"No data retrieved from RTT API for {ORIGIN_STATION_CRS}. Skipping file update.")

if __name__ == "__main__":
    main()
