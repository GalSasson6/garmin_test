import os
import logging
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
    GarminConnectAuthenticationError,
)

# Setup basic logging and load .env
load_dotenv()
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

email = os.getenv("GARMIN_EMAIL")
password = os.getenv("GARMIN_PASSWORD")
tokenstore = os.getenv("GARMINTOKENS") or "~/.garminconnect"

def get_mfa():
    """Get MFA code from user"""
    return input("Enter Garmin Connected One-Time Code: ")

def init_api():
    """Initialize Garmin API with token caching to avoid repeated logins."""
    print("Authenticating with Garmin Connect...")
    garmin = Garmin(email, password, prompt_mfa=get_mfa)

    try:
        garmin.login(tokenstore)
    except (FileNotFoundError, GarminConnectAuthenticationError) as err:
        print("Login tokens not present or expired. Please authenticate...")
        try:
            garmin.login()
            expanded_tokenstore = os.path.expanduser(tokenstore)
            os.makedirs(expanded_tokenstore, exist_ok=True)
            garmin.garth.dump(expanded_tokenstore)
        except Exception as e:
            logger.error(f"Authentication Failed: {e}")
            return None
    except Exception as e:
        logger.error(f"A general error occurred: {e}")
        return None

    return garmin

def fetch_and_plot_runs(api):
    print("Fetching the latest 200 activities (this might take a few seconds)...")
    
    # Grab a decent chunk of activities (up to 200) to find enough runs
    # You can increase this limit if you have years of data
    activities = api.get_activities(0, 200)
    
    if not activities:
        print("No activities found!")
        return

    # Filter out only the running activities
    runs = [act for act in activities if act.get('activityType', {}).get('typeKey', '') in ['running', 'treadmill_running']]
    
    if not runs:
        print("No running activities found in the last 200 activities!")
        return
        
    print(f"Found {len(runs)} recent runs. Building the graph...")

    # Extract start times
    # Garmin gives time in form of "2024-03-24 15:30:00" usually but actually startTimeLocal is string
    run_dates = []
    for run in runs:
        date_str = run.get('startTimeLocal') # Format: 2023-11-20 07:12:35
        if date_str:
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                run_dates.append(date_obj)
            except ValueError:
                pass

    if not run_dates:
        print("Could not parse dates for the runs.")
        return

    # Convert to a pandas dataframe to easily group by week
    df = pd.DataFrame({'Date': run_dates})
    
    # Group by week (W-MON means week starting on Monday)
    # Count the number of runs per week
    runs_per_week = df.groupby(pd.Grouper(key='Date', freq='W-MON')).size()
    
    # Prepare the plot
    plt.figure(figsize=(10, 6))
    
    # Create the bar chart
    ax = runs_per_week.plot(kind='bar', color='#1f77b4', edgecolor='black', zorder=2)
    
    # Format the X-axis to only show YYYY-MM-DD
    labels = [date.strftime("%Y-%m-%d") for date in runs_per_week.index]
    ax.set_xticklabels(labels, rotation=45, ha='right')

    # Add a grid and labels
    plt.grid(axis='y', linestyle='--', alpha=0.7, zorder=1)
    plt.title('Number of Runs Per Week', fontsize=16, fontweight='bold')
    plt.xlabel('Week Starting Date', fontsize=12)
    plt.ylabel('Total Runs', fontsize=12)
    
    # Ensure there are no cut-off labels
    plt.tight_layout()
    
    # Show the plot window!
    print("Graph generated! Displaying plot window...")
    plt.show()

if __name__ == "__main__":
    api = init_api()
    if api:
         fetch_and_plot_runs(api)
