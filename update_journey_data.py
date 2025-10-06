import os
import json
import logging
from datetime import datetime, timedelta, timezone
from requests import Session, HTTPError, ConnectionError, Timeout
from typing import Optional, Dict, List, Any

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
# ---------------------

# --- Configuration ---
RTT_USERNAME = os.getenv("RTT_USERNAME") # Loaded from GitHub Secret
RTT_PASSWORD = os.getenv("RTT_PASSWORD") # Loaded from GitHub Secret
OUTPUT_FILE = "live_data.json"
API_BASE_URL = "https://api.rtt.io/api/v1"

# Use the actual stations from the user's journey
ORIGIN_STATION_CRS = "SRC" # Streatham Common
CONNECTION_STATION_CRS = "CLJ" # Clapham Junction
FINAL_DESTINATION_CRS = "IMW" # Imperial Wharf
MIN_TRANSFER_MINUTES = 1 # ✅ FIX: Minimum transfer time in minutes, as requested by the user
# ---------------------

# --- Helper Functions ---
def parse_rtt_time(time_str: str) -> Optional[datetime]:
    """Parses RTT HHMM time string into a datetime object, assuming today's date."""
    if not time_str or len(time_str) != 4:
        return None
    try:
        # Combine RTT time with current date (in UTC) for accurate calculation
        now = datetime.now(timezone.utc)
        # Create a datetime object for today with the RTT time
        dt_with_time = datetime.strptime(time_str, "%H%M").replace(
            year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc
        )
        
        # Handle overnight travel (e.g., if arrival is 00:05 and departure is 23:55)
        # If departure is earlier than arrival, assume it's the next day
        if len(str(now.time()).split(':')) > 2 and dt_with_time.time() < now.time() and dt_with_time.hour < 3: # Handle late-night/early-morning services
             dt_with_time += timedelta(days=1)

        return dt_with_time
    except ValueError:
        return None

def get_real_departure_time(location_detail: Dict[str, Any]) -> str:
    """Gets the most accurate departure time (realtime or booked) and formats it to HH:MM."""
    departure_time_rtt = location_detail.get('realtimeDeparture', location_detail.get('gbttBookedDeparture'))
    if departure_time_rtt:
        return f"{departure_time_rtt[:2]}:{departure_time_rtt[2:]}"
    return "TBC"

# --- RTT Client (Code remains the same as previous step, ensuring API calls) ---
class RttClient:
    """Client for the Realtime Trains JSON API."""
    
    def __init__(self, username, password):
        if not username or not password:
             raise ValueError("RTT_USERNAME and RTT_PASSWORD must be provided.")
        
        self.session = Session()
        self.session.auth = (username, password) # Basic HTTP Auth
        self.base_url = API_BASE_URL

    def _make_request(self, endpoint):
        """Internal method to handle API requests."""
        url = self.base_url + endpoint
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status() 
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

    def get_station_departures(self, station_crs: str) -> Optional[Dict]:
        """Fetches live departures for a given station."""
        endpoint = f"/json/search/{station_crs}"
        logging.info(f"Fetching departures for {station_crs} from RTT API...")
        return self._make_request(endpoint)

    def get_service_details(self, service_uid: str, date_of_service: str) -> Optional[Dict]:
        """
        Fetches detailed information for a specific service.
        Date must be in YYYY/MM/DD format.
        """
        date_str = date_of_service.replace('-', '/')
        endpoint = f"/json/service/{service_uid}/{date_str}"
        logging.info(f"Fetching details for service {service_uid} on {date_str}...")
        return self._make_request(endpoint)

def find_calling_point(details: Dict, crs: str) -> Optional[Dict]:
    """Searches the service schedule for a specific CRS."""
    if 'locations' not in details:
        return None
    
    # Locations is a list of calling points
    for location in details['locations']:
        if location.get('crs') == crs:
            return location
    return None

def calculate_transfer_time(arrival_dt: datetime, departure_dt: datetime) -> int:
    """Calculates transfer time in minutes."""
    time_diff = departure_dt - arrival_dt
    return int(time_diff.total_seconds() / 60)

def process_rtt_data(origin_data: Dict, client: RttClient) -> List[Dict]:
    """Processes RTT data to extract the two-leg journey information with accurate transfer times."""
    
    processed_journeys = []
    current_time_str = datetime.now().strftime("%H:%M:%S")

    # 1. Fetch the CLAPHAM JUNCTION (CLJ) departure board once
    clj_departure_board = client.get_station_departures(CONNECTION_STATION_CRS)
    if not clj_departure_board or 'services' not in clj_departure_board:
        logging.warning(f"Could not retrieve departure board for {CONNECTION_STATION_CRS}. Cannot find connections.")
        # Return a metadata entry explaining the failure
        return [{"meta_data": {"rtt_status": "Failed to Fetch CLJ Board", "note": "Check RTT API Status and Credentials."}}]

    # Filter CLJ board for services heading to the final destination (IMW)
    clj_to_imw_services = [
        service for service in clj_departure_board.get('services', [])
        if service.get('destination', [{}])[0].get('crs') == FINAL_DESTINATION_CRS
    ]

    # 2. Iterate through First Leg (SRC) services
    for i, src_service in enumerate(origin_data.get('services', [])):
        service_uid = src_service.get('serviceUid')
        run_date = src_service.get('runDate')

        if not service_uid or not run_date:
            continue

        # A. Fetch Service Details for SRC service
        service_details = client.get_service_details(service_uid, run_date)
        if not service_details:
            continue

        # B. Find CLJ arrival point in service details
        clj_arrival_detail = find_calling_point(service_details, CONNECTION_STATION_CRS)
        
        # Skip if the service doesn't call at CLJ
        if not clj_arrival_detail:
            continue

        # Extract first leg data
        src_detail = src_service['locationDetail']
        src_dep_time_str = get_real_departure_time(src_detail)
        
        clj_arr_time_rtt = clj_arrival_detail.get('realtimeArrival', clj_arrival_detail.get('gbttBookedArrival'))
        clj_arr_time_str = f"{clj_arr_time_rtt[:2]}:{clj_arr_time_rtt[2:]}" if clj_arr_time_rtt else "TBC"
        clj_arr_platform = clj_arrival_detail.get('platform', 'TBC')
        
        first_leg_arrival_dt = parse_rtt_time(clj_arr_time_rtt)
        if not first_leg_arrival_dt:
             continue # Skip if CLJ arrival time is missing

        # C. Find the best connection from CLJ to IMW
        best_connection = None
        min_transfer = float('inf')
        
        for clj_service in clj_to_imw_services:
            clj_dep_rtt = clj_service['locationDetail'].get('realtimeDeparture', clj_service['locationDetail'].get('gbttBookedDeparture'))
            clj_dep_dt = parse_rtt_time(clj_dep_rtt)
            
            if not clj_dep_dt:
                continue

            transfer_minutes = calculate_transfer_time(first_leg_arrival_dt, clj_dep_dt)

            # Check for the minimum transfer time requested (>= 1 min)
            if transfer_minutes >= MIN_TRANSFER_MINUTES and transfer_minutes < min_transfer:
                min_transfer = transfer_minutes
                best_connection = clj_service
        
        # If a connection is found, process it
        connection_data = []
        total_duration = "N/A"
        arrival_time = "N/A"

        if best_connection:
            clj_dep_detail = best_connection['locationDetail']
            clj_dep_time_str = get_real_departure_time(clj_dep_detail)
            clj_dep_platform = clj_dep_detail.get('platform', 'TBC')
            
            # The IMW arrival time would require another service details call on the second leg,
            # which we'll skip to reduce API load, keeping it TBC.
            
            second_leg = {
                "origin": "Clapham Junction Rail Station",
                "destination": best_connection.get('destination', [{}])[0].get('description', 'Imperial Wharf Rail Station'),
                "departure": clj_dep_time_str,
                "arrival": "TBC (Service Details Required)", 
                "departurePlatform_Clapham": clj_dep_platform, # ✅ FIX: CLJ Departure Platform
                "operator": best_connection.get('operator', 'N/A'),
                "status": "Delayed" if clj_dep_detail.get('isDelayed', False) else "On Time"
            }
            
            connection_data.append({
                "transferTime": f"{min_transfer} min", # ✅ FIX: Real Transfer Time
                "second_leg": second_leg
            })
            
            # Simple total duration: (Second Leg Departure) - (First Leg Departure)
            first_leg_dep_dt = parse_rtt_time(src_dep_time_str.replace(':', ''))
            if first_leg_dep_dt:
                total_duration_minutes = calculate_transfer_time(first_leg_dep_dt, clj_dep_dt)
                total_duration = f"{total_duration_minutes} min"
            else:
                total_duration = "N/A"

        else:
            # If no connection found
            connection_data.append({
                "transferTime": "No Connection Found (>1 min transfer needed)",
                "second_leg": {
                    "origin": "Clapham Junction Rail Station",
                    "destination": "Imperial Wharf Rail Station",
                    "departure": "N/A",
                    "arrival": "N/A",
                    "departurePlatform_Clapham": "N/A",
                    "operator": "N/A",
                    "status": "No Connection Found"
                }
            })

        # Structure the final journey object
        processed_journeys.append({
            "type": "Live RTT Update (Dual API)",
            "first_leg": {
                "origin": 'Streatham Common Rail Station',
                "destination": service_details.get('destination', [{}])[0].get('description', CONNECTION_STATION_CRS),
                "departure": src_dep_time_str,
                "scheduled_departure": src_dep_time_str,
                "arrival": clj_arr_time_str, # ✅ FIX: CLJ Arrival Time
                "departurePlatform_Streatham": src_detail.get('platform', 'TBC'),
                "arrivalPlatform_Clapham": clj_arr_platform, # ✅ FIX: CLJ Arrival Platform
                "operator": service_details.get('operator', 'N/A'),
                "status": "Delayed" if src_detail.get('isDelayed', False) else "On Time",
                "serviceUid": service_uid,
                "dateOfService": run_date
            },
            "connections": connection_data,
            "totalDuration": total_duration,
            "arrivalTime": arrival_time,
            "departureTime": src_dep_time_str,
            "segment_id": len(processed_journeys) + 1,
            "live_updated_at": current_time_str
        })
        
        # Limiting to the first 3 services to match the common webpage output and limit API calls
        if len(processed_journeys) >= 3:
            break

    # Insert metadata
    meta_data = {
        "rtt_status": "Accurate Two-Leg Data (Transfer Logic Applied)", 
        "note": "CLJ arrival platform/time is accurate via Service Details API. Next connection to IMW is accurately selected with a minimum 1 minute transfer time."
    }
    processed_journeys.insert(0, {"meta_data": meta_data})

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
        processed_data = process_rtt_data(live_data_raw, client)
        
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
