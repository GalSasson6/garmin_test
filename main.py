import os
import logging
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
    GarminConnectAuthenticationError,
)

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get credentials from environment variables
email = os.getenv("GARMIN_EMAIL")
password = os.getenv("GARMIN_PASSWORD")

# Directory to store the session tokens
tokenstore = os.getenv("GARMINTOKENS") or "~/.garminconnect"
tokenstore_base64 = os.getenv("GARMINTOKENS_BASE64") or "~/.garminconnect_base64"


def get_mfa():
    """Get MFA code from user"""
    return input("Enter Garmin Connected One-Time Code: ")

def init_api():
    """Initialize Garmin API with token caching to avoid repeated logins."""
    print(f"Trying to login to Garmin Connect using token data from directory '{tokenstore}'...")
    
    # Initialize Garmin object with credentials and MFA prompt
    garmin = Garmin(email, password, prompt_mfa=get_mfa)

    try:
        # Try to use tokens, if missing Garmin throws internal exceptions
        garmin.login(tokenstore)
    except (FileNotFoundError, GarminConnectAuthenticationError) as err:
        # If tokens don't exist, or are expired, we must login using email/password + MFA
        print(
            "Login tokens not present or expired, logging in with email and password to create new tokens..."
        )
        try:
            # Login without tokenstore will trigger MFA if needed
            garmin.login()

            # Save the newly generated tokens for next time
            expanded_tokenstore = os.path.expanduser(tokenstore)
            os.makedirs(expanded_tokenstore, exist_ok=True)
            print(f"Saving tokens to '{expanded_tokenstore}' for future faster and more secure logins...")
            garmin.garth.dump(expanded_tokenstore)
        except GarminConnectAuthenticationError as err:
            logger.error(f"Failed to authenticate. Are email/password correct? Error: {err}")
            return None
        except Exception as err:
             logger.error(f"A general error occurred: {err}")
             return None

    except Exception as err:
        logger.error(f"A general error occurred: {err}")
        return None

    return garmin


if __name__ == "__main__":
    api = init_api()

    if api:
        print("Login successful! Secure connection established.")
        try:
             # Fetch some basic profile data to verify it works
             full_name = api.get_full_name()
             print(f"\nHello, {full_name}!")
             
             print("\nFetching your 5 most recent activities...")
             activities = api.get_activities(0, 5) # Get the 5 most recent activities
             
             for activity in activities:
                 name = activity.get('activityName', 'Unknown')
                 activity_type = activity.get('activityType', {}).get('typeKey', 'unknown')
                 
                 # Distance is usually in meters, convert to KM
                 distance_meters = activity.get('distance', 0)
                 distance_km = distance_meters / 1000 if distance_meters else 0
                 
                 # Duration is usually in seconds, convert to minutes
                 duration_seconds = activity.get('duration', 0)
                 duration_minutes = duration_seconds / 60 if duration_seconds else 0
                 
                 print(f" - [{activity_type.upper()}] {name}: {distance_km:.2f} km in {duration_minutes:.1f} mins")
                 
        except Exception as e:
             print(f"Failed to fetch data: {e}")
    else:
        print("Login failed.")

