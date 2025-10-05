import os
import json
from datetime import datetime, timedelta
# Import lxml for manual header construction
from lxml import etree 
from zeep import Client, Settings, Transport
from zeep.exceptions import Fault
from requests import Session

# --- Configuration ---
# Your NRE OpenLDBWS Token (Use environment variable for security in production)
LDB_TOKEN = os.getenv("LDB_TOKEN", "8aaaf362-b5d6-4886-c123-08e137bd4a7b") # Replace with your token if testing locally
OUTPUT_FILE = "live_data.json"

# NRE Station CRS Codes
ORIGIN_CRS = "STR" # Streatham Common
INTERCHANGE_CRS = "CLJ" # Clapham Junction
DESTINATION_CRS = "IMW" # Imperial Wharf

# User-facing Station Names (for output consistency with TFL script)
ORIGIN_NAME = "Streatham Common Rail Station"
INTERCHANGE_NAME = "Clapham Junction Rail Station"
DESTINATION_NAME = "Imperial Wharf Rail Station"

# OpenLDBWS WSDL URL and parameters
WSDL_URL = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01"
NUM_ROWS = 15 # Number of services to fetch per board
TIME_WINDOW_MINUTES = 120 # How far into the future to look (2 hours)
MIN_TRANSFER_TIME_MINUTES = 1 # Minimum acceptable transfer time
MAX_TRANSFER_TIME_MINUTES = 5 # Maximum transfer time to enforce the "short transfer" logic
NUM_JOURNEYS = 8 # Target the next eight best segments (Direct or One Change)

# --- NRE Client ---
class NreLdbClient:
    """Client for the National Rail OpenLDBWS SOAP API."""
    def __init__(self, token):
        if not token:
            raise ValueError("LDB_TOKEN must be provided.")
        
        self.token = token
        
        # Configure Zeep for WSDL caching and set up the SOAP Header with the AccessToken
        session = Session()
        settings = Settings(strict=False, xml_huge_tree=True)
        transport = Transport(session=session)
        self.client = Client(WSDL_URL, transport=transport, settings=settings)
        
        # --- FIX: Manually construct the SOAP header using lxml.etree ---
        # This bypasses the zeep serialization bug and reliably creates the header:
        # <AccessToken xmlns="http://thalesgroup.com/RTTI/2013-11-28/Token/types">
        #    <TokenValue>YOUR_TOKEN</TokenValue>
        # </AccessToken>
        
        NS_TOKEN = 'http://thalesgroup.com/RTTI/2013-11-28/Token/types'
        
        # 1. Create the AccessToken element with the correct namespace
        token_access = etree.Element(etree.QName(NS_TOKEN, 'AccessToken'))
        
        # 2. Create the TokenValue sub-element and set its text to the actual token
        token_value = etree.SubElement(token_access, 'TokenValue')
        token_value.text = self.token
        
        # Set the manually constructed element as the header
        self.header = token_access
        # --- END FIX ---


    def get_departure_board_with_details(self, crs, filter_crs=None):
        """Calls the GetDepBoardWithDetails API method."""
        try:
            # We pass the manually constructed lxml element here
            board = self.client.service.GetDepBoardWithDetails(
                _soapheaders={'AccessToken': self.header},
                numRows=NUM_ROWS,
                crs=crs,
                timeWindow=TIME_WINDOW_MINUTES,
                filterCrs=filter_crs if filter_crs else None, # Filter to a specific destination
                filterType='to' if filter_crs else 'from'
            )
            return board
        except Fault as e:
            # Print the fault details, often helpful for debugging
            print(f"ERROR: SOAP Fault occurred for CRS {crs}: {e}")
            return None
        except Exception as e:
            # Catch other connection/serialization errors
            print(f"ERROR: Failed to connect or retrieve data for CRS {crs}: {e}")
            return None

# --- Data Processing Functions (Remainder of file is unchanged) ---

def parse_nre_service(service_data, origin_crs, destination_crs=None):
    """
    Parses a single NRE service object into a standard leg dict.
    This function handles finding the specific stop points (origin, destination) within the service.
    """
    
    # 1. Get Scheduled/Estimated Times and Platform at the Origin
    origin_time = service_data.std
    estimated_departure = service_data.etd
    platform = service_data.platform if service_data.platform else "TBC"

    # 2. Get Estimated/Scheduled Times at the Destination/Interchange
    arrival_time = None
    
    # Check calling points for the destination_crs
    if destination_crs:
        # The API returns a list of CallingPointList, but we only expect one for a single journey
        calling_points_lists = getattr(service_data.subsequentCallingPoints, 'callingPointList', [])
        
        for calling_points in calling_points_lists:
            for point in getattr(calling_points, 'callingPoint', []):
                if point.crs == destination_crs:
                    # Use 'eta' (Estimated Time of Arrival) if available, otherwise 'sta' (Scheduled Time of Arrival)
                    arrival_time = point.eta if point.eta not in ["On time", "Delayed", "Cancel"] else point.sta
                    break
            if arrival_time:
                break
    
    # If a specific destination was requested but not found, this service doesn't stop there.
    if destination_crs and not arrival_time:
        return None 
    
    # If no destination_crs, assume it's the final terminus reported by the API (which is service_data.destination)
    if not destination_crs:
        # Use arrival time from the main service destination if no calling points were specified
        arrival_time = service_data.sta

    # Check for cancellation/delay status
    status = estimated_departure
    if estimated_departure in ["Delayed", "Cancel"]:
        status = estimated_departure
    elif estimated_departure == origin_time:
        status = "On Time"
        
    leg_data = {
        "scheduled_departure": origin_time,
        "departure": estimated_departure if estimated_departure not in ["On time", "Delayed", "Cancel"] else origin_time,
        "arrival": arrival_time,
        "platform": platform,
        "operator": service_data.operator,
        "status": status,
        "service_id": service_data.serviceID, # Use NRE service ID for unique identification
        "train_destination": getattr(service_data.destination.location[0], 'locationName', 'Unknown Destination')
    }
    return leg_data

def combine_and_format_journeys(direct_data, stitched_data):
    """Combines, sorts, and formats the final output data."""
    
    combined_data = direct_data + stitched_data
    
    # Sort the list by the departure time of the first leg/direct journey
    # This ensures the list is ordered chronologically
    def sort_key(item):
        # Fallback to scheduled departure if live departure is "Cancel" or similar
        dep_time_str = item['first_leg']['departure']
        try:
            return datetime.strptime(dep_time_str, '%H:%M')
        except ValueError:
            # Handle cases where 'departure' is a status string (e.g., 'Cancel')
            return datetime.strptime(item['first_leg']['scheduled_departure'], '%H:%M')


    sorted_data = sorted(combined_data, key=sort_key)
    
    # Add IDs, Timestamps, and slice the final list
    final_output = []
    current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for idx, segment in enumerate(sorted_data):
        segment['segment_id'] = idx + 1
        segment['live_updated_at'] = current_time_str
        final_output.append(segment)

    # Limit to NUM_JOURNEYS segments
    return final_output[:NUM_JOURNEYS]

# --- Main Logic Functions ---

def get_direct_journeys(client):
    """Fetches direct STR -> IMW services and formats them."""
    print(f"Fetching Direct Services: {ORIGIN_CRS} -> {DESTINATION_CRS}...")
    
    # Use GetDepBoardWithDetails from ORIGIN, filtered TO DESTINATION
    board = client.get_departure_board_with_details(ORIGIN_CRS, filter_crs=DESTINATION_CRS)
    if not board or not getattr(board.trainServices, 'service', None):
        print("No direct services found.")
        return []

    direct_journeys = []
    for service in board.trainServices.service:
        # Pass DESTINATION_CRS to find the specific arrival time at Imperial Wharf
        leg_data = parse_nre_service(service, ORIGIN_CRS, destination_crs=DESTINATION_CRS)
        
        if leg_data:
            journey = {
                "type": "Direct",
                "first_leg": {
                    "origin": ORIGIN_NAME,
                    "destination": DESTINATION_NAME,
                    "scheduled_departure": leg_data['scheduled_departure'],
                    "departure": leg_data['departure'],
                    "arrival": leg_data['arrival'],
                    "departurePlatform_Streatham": leg_data['platform'],
                    "arrivalPlatform_Imperial": leg_data['platform'], # Platform remains the same for direct services
                    "operator": leg_data['operator'],
                    "status": leg_data['status'],
                    "train_destination": leg_data['train_destination']
                },
                "connections": [],
                "totalDuration": "N/A", # Will be calculated later if needed, but TFL style leaves it N/A
                "arrivalTime": leg_data['arrival'],
                "departureTime": leg_data['departure'],
            }
            direct_journeys.append(journey)
            
    print(f"Found {len(direct_journeys)} direct services.")
    return direct_journeys

def get_one_change_journeys(client):
    """
    Fetches STR -> CLJ and CLJ -> IMW services and stitches them based on short transfer time.
    """
    print(f"Fetching and Stitching One-Change Journeys: {ORIGIN_CRS} -> {INTERCHANGE_CRS} -> {DESTINATION_CRS}...")

    # 1. Fetch First Leg (L1): Streatham Common (STR) to Clapham Junction (CLJ)
    # We must fetch the full board from STR and look for the calling point at CLJ.
    board_l1 = client.get_departure_board_with_details(ORIGIN_CRS)
    l1_services = []
    if board_l1 and getattr(board_l1.trainServices, 'service', None):
        for service in board_l1.trainServices.service:
            # Pass CLJ as the destination_crs to find the correct arrival time/platform at the interchange
            leg = parse_nre_service(service, ORIGIN_CRS, destination_crs=INTERCHANGE_CRS)
            if leg:
                # Add the train's unique service ID to use as a primary key for the first leg
                leg['unique_id'] = service.serviceID 
                l1_services.append(leg)
    print(f"Found {len(l1_services)} valid L1 legs ({ORIGIN_CRS} -> {INTERCHANGE_CRS}).")

    # 2. Fetch Second Leg (L2): Clapham Junction (CLJ) to Imperial Wharf (IMW)
    # We must fetch the full board from CLJ and look for the calling point at IMW.
    board_l2 = client.get_departure_board_with_details(INTERCHANGE_CRS)
    l2_services = []
    if board_l2 and getattr(board_l2.trainServices, 'service', None):
        for service in board_l2.trainServices.service:
            # Pass IMW as the destination_crs to find the correct arrival time at the destination
            leg = parse_nre_service(service, INTERCHANGE_CRS, destination_crs=DESTINATION_CRS)
            if leg:
                l2_services.append(leg)
    print(f"Found {len(l2_services)} valid L2 legs ({INTERCHANGE_CRS} -> {DESTINATION_CRS}).")

    # 3. Stitch Legs and Filter by Transfer Time
    stitched_journeys = {} # Use dict to group connections by L1 service
    
    # Get current time for time arithmetic base
    now = datetime.now()

    for l1 in l1_services:
        try:
            l1_arr_time = datetime.strptime(l1['arrival'], '%H:%M')
        except ValueError:
            # Skip services with missing/invalid arrival time
            continue 
        
        # We need to account for services arriving just before midnight
        # Simple logic: If L1 arrives in the early morning (e.g., < 3 AM) and it's currently evening, 
        # assume it's the next day's service.
        if l1_arr_time.hour < 3 and now.hour > 20: 
             l1_arr_time += timedelta(days=1)
        
        # Create a base journey structure using the L1 leg data
        l1_journey_base = {
            "type": "One Change",
            "first_leg": {
                "origin": ORIGIN_NAME,
                "destination": INTERCHANGE_NAME,
                "scheduled_departure": l1['scheduled_departure'],
                "departure": l1['departure'],
                "arrival": l1['arrival'],
                "departurePlatform_Streatham": l1['platform'],
                "arrivalPlatform_Clapham": "TBC", # LDB does not provide arrival platform at intermediate stops
                "operator": l1['operator'],
                "status": l1['status'],
                "train_destination": l1['train_destination']
            },
            "connections": [],
            "departureTime": l1['departure'],
        }

        for l2 in l2_services:
            try:
                l2_dep_time = datetime.strptime(l2['departure'], '%H:%M')
            except ValueError:
                # Skip services with missing/invalid departure time
                continue

            # Account for L2 departures after midnight on the same "run"
            if l2_dep_time < l1_arr_time and l1_arr_time.hour > 20 and l2_dep_time.hour < 3:
                l2_dep_time += timedelta(days=1)

            # Calculate the transfer time
            transfer_duration = l2_dep_time - l1_arr_time
            transfer_minutes = transfer_duration.total_seconds() / 60
            
            # Apply the user's short-transfer-only filter
            if MIN_TRANSFER_TIME_MINUTES <= transfer_minutes <= MAX_TRANSFER_TIME_MINUTES:
                
                # Calculate total duration (for display)
                try:
                    l1_dep_time_obj = datetime.strptime(l1['departure'], '%H:%M')
                    l2_arr_time_obj = datetime.strptime(l2['arrival'], '%H:%M')
                except ValueError:
                    # Fallback to scheduled times for duration calculation if live times are bad
                    l1_dep_time_obj = datetime.strptime(l1['scheduled_departure'], '%H:%M')
                    l2_arr_time_obj = datetime.strptime(l2['arrival'], '%H:%M')
                    
                # Handle overnight duration correctly
                if l2_arr_time_obj < l1_dep_time_obj:
                    l2_arr_time_obj += timedelta(days=1)

                total_duration_sec = (l2_arr_time_obj - l1_dep_time_obj).total_seconds()
                
                # Format the L2 leg as a connection
                connection = {
                    "transferTime": f"{int(transfer_minutes)} min",
                    "second_leg": {
                        "origin": INTERCHANGE_NAME,
                        "destination": DESTINATION_NAME,
                        "departure": l2['departure'],
                        "arrival": l2['arrival'],
                        "departurePlatform_Clapham": l2['platform'], # Platform at Clapham for the L2 leg
                        "operator": l2['operator'],
                        "status": l2['status'],
                        "train_destination": l2['train_destination']
                    }
                }
                
                # Group connection by the first leg's service ID (l1['unique_id'])
                if l1['unique_id'] not in stitched_journeys:
                    # Initialize the L1 journey base and total duration/arrival
                    base_journey = dict(l1_journey_base)
                    base_journey['connections'].append(connection)
                    # Use the first connection's arrival/duration as the primary value for the segment
                    base_journey['arrivalTime'] = l2['arrival']
                    base_journey['totalDuration'] = f"{int(total_duration_sec // 60)} min"
                    stitched_journeys[l1['unique_id']] = base_journey
                else:
                    # Append subsequent connections to the existing L1 journey
                    stitched_journeys[l1['unique_id']]['connections'].append(connection)

    # Convert the dictionary values (grouped journeys) back to a list
    final_stitched_list = list(stitched_journeys.values())
    print(f"Stitched {len(final_stitched_list)} journeys with 1-{MAX_TRANSFER_TIME_MINUTES} min transfers.")
    return final_stitched_list


def main():
    """Main execution function to fetch, process, and save data."""
    if LDB_TOKEN == "YOUR_TOKEN": # Safety check if user didn't update config
        print("ERROR: Please set the LDB_TOKEN environment variable with your NRE token.")
        return

    client = NreLdbClient(LDB_TOKEN)
    
    # 1. Get all Direct Journeys
    direct_data = get_direct_journeys(client) 
    
    # 2. Get all One-Change Journeys
    stitched_data = get_one_change_journeys(client)
    
    # 3. Combine and Sort all results
    final_output = combine_and_format_journeys(direct_data, stitched_data)
    
    # 4. Save to JSON
    if final_output:
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(final_output, f, indent=4)
        print(f"\n✅ Successfully saved {len(final_output)} journey segments (Max {NUM_JOURNEYS}) to {OUTPUT_FILE}")
    else:
        print(f"\n❌ No services found and saved to {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
