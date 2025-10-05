import os
import json
import logging
from datetime import datetime, timedelta
from lxml import etree 
from zeep import Client, Settings, Transport
from zeep.exceptions import Fault
from zeep.plugins import Plugin
from requests import Session

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logging.getLogger('zeep.transports').setLevel(logging.DEBUG) 
# ---------------------

# --- Configuration ---
# ✅ SECURE FIX: This loads the secret from the environment (GitHub Secret LDB_TOKEN).
# It falls back to an invalid placeholder string only if the environment variable is not set.
LDB_TOKEN = os.getenv("LDB_TOKEN", "INVALID_LDB_TOKEN_PLACEHOLDER") 
OUTPUT_FILE = "live_data.json"
# ... (rest of configuration is the same)
# ...

# --- Zeep Plugin for Header Injection (The most robust authentication method) ---
class NreHeaderPlugin(Plugin):
    """
    Plugin to inject the custom lxml-constructed AccessToken header into the SOAP envelope.
    """
    def __init__(self, header_element):
        self.header_element = header_element

    def egress(self, envelope, http_headers, operation, binding_options):
        header = envelope.find('soap-env:Header', namespaces=envelope.nsmap)
        if header is not None:
            header.append(self.header_element)
        return envelope, http_headers

# --- NRE Client ---
class NreLdbClient:
    """Client for the National Rail OpenLDBWS SOAP API."""
    def __init__(self, token):
        if not token or token == "INVALID_LDB_TOKEN_PLACEHOLDER": # Check if the token is the placeholder
            # If the script failed to find the secret, it will fail here, prompting the user to check the workflow file
            logging.error("LDB_TOKEN is not set or is using the invalid placeholder. Please check your GitHub Secrets and workflow file.")
            raise ValueError("LDB_TOKEN must be provided via environment variable.")
        
        # ... (rest of __init__ is the same)
        # ...
        
# ... (rest of the file remains the same)
# ...

def main():
    """Main execution function to fetch, process, and save data."""
    # The check now happens inside NreLdbClient.__init__
    
    try:
        client = NreLdbClient(LDB_TOKEN)
    except ValueError as e:
        print(f"\n❌ FAILED TO INITIALIZE: {e}")
        return # Exit the script if the token is missing/invalid
        
    # 1. Get all Direct Journeys
    # ... (rest of main function is the same)
