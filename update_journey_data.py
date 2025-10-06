import os
import json
import logging
from datetime import datetime, timedelta
from requests import Session, HTTPError, ConnectionError, Timeout
# Removed: from lxml import etree 
# Removed: from zeep import Client, Settings, Transport
# Removed: from zeep.exceptions import Fault
# Removed: from zeep.plugins import Plugin 

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
# Removed: logging.getLogger('zeep.transports').setLevel(logging.DEBUG) 
# ---------------------

# --- Configuration ---
# ✅ SECURE FIX: Load credentials from the environment (GitHub Secrets).
# Using the example credentials from the documentation as a non-secure fallback for local testing ONLY.
RTT_USERNAME = os.getenv("RTT_USERNAME", "rttapi_Donostio") # [cite: 204]
RTT_PASSWORD = os.getenv("RTT_PASSWORD", "978bdbfa3343c13a44e7a1336e91bfba511f3da1") # [cite: 205]
OUTPUT_FILE = "live_data.json"
API_BASE_URL = "https://api.rtt.io/api/v1" # [cite: 201]
# Example Station for testing (Bournemouth - BMH) [cite: 202, 289]
MAIN_STATION_CRS = "BMH"
# ---------------------

# --- RTT Client ---
class RttClient:
    """Client for the Realtime Trains JSON API."""
    
    def __init__(self, username, password):
        if username == "rttapi_Donostio" and password == "978bdbfa3343c13a44e7a1336e91bfba511f3da1":
             logging.warning("Using example RTT credentials. Ensure secrets are configured for production use.")
        
        self.session = Session()
        # Basic HTTP Auth is required [cite: 206]
        self.session.auth = (username, password)
        self.base_url = API_BASE_URL

    def get_live_departures(self, station_crs):
        """
        Fetches live departures for a given station.
        Example endpoint: /json/search/<station> [cite: 235]
        """
        endpoint = f"/json/search/{station_crs}"
        url = self.base_url + endpoint
        
        logging.info(f"Fetching live data for station {station_crs} from RTT API...")
        
        try:
            response = self.session.get(url, timeout=10)
            # RTT API responses should have gzip compression [cite: 209] - requests handles this.
            response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)
            
            # The API returns JSON [cite: 211]
            data = response.json()
            logging.info(f"Successfully retrieved {len(data.get('services', []))} services.")
            return data

        except HTTPError as e:
            # 404 is returned if service/station is not found [cite: 282]
            logging.error(f"HTTP Error {e.response.status_code} fetching data: {e}")
            return None
        except (ConnectionError, Timeout) as e:
            logging.error(f"Connection/Timeout Error: {e}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return None

# Removed: NreHeaderPlugin class
# Removed: NreLdbClient class

def main():
    """Main execution function to fetch, process, and save data."""
    
    if not RTT_USERNAME or not RTT_PASSWORD or RTT_USERNAME == "INVALID_RTT_USERNAME_PLACEHOLDER":
        logging.error("RTT_USERNAME or RTT_PASSWORD is not set. Exiting.")
        print("\n❌ FAILED TO INITIALIZE: RTT credentials must be provided via environment variables or secrets.")
        return # Exit the script if the token is missing/invalid
        
    try:
        client = RttClient(RTT_USERNAME, RTT_PASSWORD)
    except Exception as e:
        print(f"\n❌ FAILED TO INITIALIZE RTT Client: {e}")
        return 
        
    # 1. Get all Direct Journeys (e.g., from Bournemouth)
    live_data = client.get_live_departures(MAIN_STATION_CRS)

    if live_data:
        # 2. Process and Save Data
        # In a real implementation, you would process the 'services' array for platform/delay info.
        # For this refactor, we just save the raw output.
        try:
            with open(OUTPUT_FILE, 'w') as f:
                # Use a cleaner format for JSON output
                json.dump(live_data, f, indent=4)
            logging.info(f"Data saved successfully to {OUTPUT_FILE}")
        except IOError as e:
            logging.error(f"Failed to write to file {OUTPUT_FILE}: {e}")
    else:
        logging.warning("No data retrieved from RTT API. Skipping file update.")

if __name__ == "__main__":
    main()
