import os
import json
import logging
import time
from datetime import datetime
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
import folium
import osmnx as ox
from geopy.geocoders import Nominatim
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectAuthenticationError,
)

# Setup basic logging and load .env
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CACHE_DIR = ".garmin_cache"
POLYLINES_DIR = os.path.join(CACHE_DIR, "polylines")
os.makedirs(POLYLINES_DIR, exist_ok=True)

# Garmin login
email = os.getenv("GARMIN_EMAIL")
password = os.getenv("GARMIN_PASSWORD")
tokenstore = os.getenv("GARMINTOKENS") or "~/.garminconnect"

def get_mfa():
    return input("Enter Garmin Connected One-Time Code: ")

def init_api():
    print("Authenticating with Garmin Connect...")
    garmin = Garmin(email, password, prompt_mfa=get_mfa)
    try:
        garmin.login(tokenstore)
    except (FileNotFoundError, GarminConnectAuthenticationError):
        print("Login tokens not present or expired. Re-authenticating...")
        try:
            garmin.login()
            garmin.garth.dump(os.path.expanduser(tokenstore))
        except Exception as e:
            logger.error(f"Authentication Failed: {e}")
            return None
    except Exception as e:
        logger.error(f"A general error occurred: {e}")
        return None
    return garmin

# Caching for reverse geocoding
def get_city_name(lat, lon):
    cache_file = os.path.join(CACHE_DIR, "city_cache.json")
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            city_cache = json.load(f)
    else:
        city_cache = {}
        
    coords_key = f"{round(lat, 3)},{round(lon, 3)}" # 3 decimal places ~100m grid for city resolution
    if coords_key in city_cache:
        return city_cache[coords_key]
        
    try:
        geolocator = Nominatim(user_agent="garmin_city_mapper", timeout=10)
        location = geolocator.reverse(f"{lat}, {lon}", language="en")
        
        # Parse city from address
        address = location.raw.get("address", {})
        city = address.get("city") or address.get("town") or address.get("village") or address.get("municipality")
        
        # If still None, just take the first component as a string
        if not city:
             city = location.address.split(",")[0]
             
        # cache the result
        city_cache[coords_key] = city
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(city_cache, f, ensure_ascii=False)
            
        time.sleep(1) # respect Nominatim ratelimits
        return city
    except Exception as e:
        logger.warning(f"Failed to geocode {lat}, {lon}: {e}")
        return "Unknown"

def main():
    api = init_api()
    if not api:
        return
        
    print("Fetching activities summaries (from cache if new ones don't exist)...")
    runs_file = os.path.join(CACHE_DIR, "runs_summary.json")
    
    # Simple cache logic: you can add an argument to force refresh
    if os.path.exists(runs_file):
        with open(runs_file, "r") as f:
            all_activities = json.load(f)
        print(f"Loaded {len(all_activities)} activities from local cache.")
    else:
        all_activities = []
        # Fetching 500 at a time, up to 2000 activities
        for i in range(0, 2000, 500):
            print(f" Requesting Garmin API for activities {i} to {i+500}...")
            chunk = api.get_activities(i, 500)
            if not chunk:
                break
            all_activities.extend(chunk)
            if len(chunk) < 500:
                break
            time.sleep(1)
        with open(runs_file, "w") as f:
            json.dump(all_activities, f)
            
    # Filter only runs
    runs = [act for act in all_activities if act.get('activityType', {}).get('typeKey', '') in ['running']]
    print(f"Found {len(runs)} outdoor runs. Processing GPS data...")

    m = folium.Map(location=[32.0, 34.8], zoom_start=8, tiles="CartoDB positron")
    
    cities_lines = {}
    
    has_plotted_any = False
    
    # Process each run
    for idx, run in enumerate(runs):
        act_id = str(run['activityId'])
        poly_file = os.path.join(POLYLINES_DIR, f"{act_id}.json")
        
        if os.path.exists(poly_file):
            with open(poly_file, "r") as f:
                polyline = json.load(f)
        else:
            try:
                # Fetch detailed metrics containing the GPS path
                print(f"  Fetching GPS path for run {idx}/{len(runs)} (ID: {act_id})...")
                details = api.get_activity_details(act_id)
                # Parse polyline
                poly_data = details.get("geoPolylineDTO", {}).get("polyline", [])
                polyline = [(pt["lat"], pt["lon"]) for pt in poly_data]
                
                with open(poly_file, "w") as f:
                    json.dump(polyline, f)
                time.sleep(1) # prevent rate limit
            except Exception as e:
                logger.error(f"Failed to get details for {act_id}: {e}")
                polyline = []
                
        if len(polyline) > 1:
            start_lat, start_lon = polyline[0]
            city_name = get_city_name(start_lat, start_lon)
            
            if city_name not in cities_lines:
                cities_lines[city_name] = []
                
            cities_lines[city_name].append(polyline)
            
            # Draw line on map
            folium.PolyLine(
                polyline,
                weight=3,
                color="red",
                opacity=0.6,
                tooltip=f"{run.get('activityName', 'Run')} in {city_name}"
            ).add_to(m)
            
            # Recenter map to the last plotted run
            if not has_plotted_any:
                m.location = [start_lat, start_lon]
                m.zoom_start = 12
                has_plotted_any = True

    # Save map
    map_file = os.path.join(os.getcwd(), "my_runs_map.html")
    m.save(map_file)
    print(f"\n✅ Created interactive map: {map_file}")
    
    # --- CALCULATE CITY COVERAGE ---
    print("\n--- CALCULATING CITY COMPLETION % ---")
    print("(This uses a 20m buffer radius footprint and compares it to the city's geographical area)")
    
    # Define an EPSG projection for Israel/Middle East to measure square meters properly
    # EPSG:32636 or Web Mercator (EPSG:3857) works. Let's use 3857 for simplicity.
    CRS_METERS = "EPSG:3857"
    
    for city, lines in cities_lines.items():
        if city == "Unknown":
            continue
            
        print(f"\nAnalyzing coverage for '{city}'...")
        try:
            # Create Shapely LineStrings (lon, lat)
            shapely_lines = [LineString([(lon, lat) for lat, lon in path]) for path in lines]
            
            # Create a GeoDataFrame of all runs in the city
            gdf_runs = gpd.GeoDataFrame(geometry=shapely_lines, crs="EPSG:4326")
            
            # Convert to a meter-based projection
            gdf_runs_m = gdf_runs.to_crs(CRS_METERS)
            
            # Buffer by 20 meters (approx route visibility width) & union overlapping buffers
            # unary_union dissolves overlapping areas so we don't double count!
            coverage_footprint = gdf_runs_m.geometry.buffer(20).unary_union
            covered_area_sq_m = coverage_footprint.area
            covered_area_sq_km = covered_area_sq_m / 1_000_000
            
            # Fetch City Boundaries from OpenStreetMap
            try:
                city_gdf = ox.geocode_to_gdf(f"{city}, Israel")
                city_gdf_m = city_gdf.to_crs(CRS_METERS)
                city_area_sq_m = city_gdf_m.geometry.unary_union.area
                city_area_sq_km = city_area_sq_m / 1_000_000
                
                percentage_covered = (covered_area_sq_m / city_area_sq_m) * 100
                
                print(f" -> You have covered {covered_area_sq_km:.2f} sq km of roads/paths in {city}!")
                print(f" -> Total {city} area is {city_area_sq_km:.2f} sq km.")
                print(f" -> 🏆 City Completion: {percentage_covered:.4f}%!")
                
            except Exception as e:
                # If OSM fails to find exact boundary of the city string
                print(f" -> You have covered {covered_area_sq_km:.2f} sq km of roads/paths in {city}!")
                print(f"    (Could not fetch exact city boundary from OSM for '{city}' to get percentage. Continuing...)")
                
        except Exception as e:
             logger.error(f"Error analyzing {city}: {e}")

    # Launch file in browser if possible
    import webbrowser
    webbrowser.open(map_file)
    print("\n✅ All done! Map should open in your browser.")

if __name__ == "__main__":
    main()
