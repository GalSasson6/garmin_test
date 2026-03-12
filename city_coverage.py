import os
import json
import logging
import geopandas as gpd
from shapely.geometry import LineString
from shapely.ops import linemerge, unary_union
import folium
import osmnx as ox
import warnings

# Suppress the union_all warnings
warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CACHE_DIR = ".garmin_cache"
POLYLINES_DIR = os.path.join(CACHE_DIR, "polylines")
CRS_METERS = "EPSG:3857" # Meter-based projection
BUFFER_RADIUS_METERS = 30 # How close to a street you need to be to "cover" it

# Set this to the city you want to analyze, or None to loop them all
TARGET_CITY = "Raanana"

def merged_line_length_m(geometries):
    """
    Compute unique line length by dissolving and merging overlaps.
    This avoids double counting from duplicated graph directions/segments.
    """
    valid_lines = [geom for geom in geometries if geom is not None and not geom.is_empty]
    if not valid_lines:
        return 0.0

    dissolved = unary_union(valid_lines)
    merged = linemerge(dissolved)
    return merged.length

def main():
    print(f"Preparing to analyze street coverage for {TARGET_CITY}...")
    
    # 1. Load the run history and cached cities
    runs_file = os.path.join(CACHE_DIR, "runs_summary.json")
    if not os.path.exists(runs_file):
        print("Runs cache not found. Please run the previous map script first.")
        return
        
    with open(runs_file, "r") as f:
        all_activities = json.load(f)
        
    runs = [act for act in all_activities if act.get('activityType', {}).get('typeKey', '') in ['running']]
    
    # 2. Re-process which runs belong to which city based on our city cache
    city_cache_file = os.path.join(CACHE_DIR, "city_cache.json")
    if os.path.exists(city_cache_file):
        with open(city_cache_file, "r", encoding="utf-8") as f:
            city_cache = json.load(f)
    else:
        city_cache = {}

    city_run_paths = []
    total_ran_distance_m = 0
    
    for run in runs:
        act_id = str(run['activityId'])
        poly_file = os.path.join(POLYLINES_DIR, f"{act_id}.json")
        
        if os.path.exists(poly_file):
            with open(poly_file, "r") as f:
                polyline = json.load(f)
                
            if len(polyline) > 1:
                start_lat, start_lon = polyline[0]
                coords_key = f"{round(start_lat, 3)},{round(start_lon, 3)}"
                
                # Check what city this run is in
                city_name = city_cache.get(coords_key, "Unknown")
                
                # If we are targeting a specific city
                if TARGET_CITY and (TARGET_CITY.lower() in city_name.lower()):
                    city_run_paths.append(polyline)
                    total_ran_distance_m += run.get('distance', 0) or 0

    if not city_run_paths:
        print(f"No runs found in cache for {TARGET_CITY}.")
        return
        
    print(f"Loaded {len(city_run_paths)} runs in {TARGET_CITY}.")
    
    # Convert runs into Shapely Geometries (coordinates are (lon, lat) in shapefiles usually)
    shapely_lines = [LineString([(lon, lat) for lat, lon in path]) for path in city_run_paths]
    gdf_runs = gpd.GeoDataFrame(geometry=shapely_lines, crs="EPSG:4326")
    
    # 3. Fetch the walkable street network for the city
    print(f"Downloading OpenStreetMap walkable street network for '{TARGET_CITY}, Israel'...")
    try:
        # We specify "Israel" to help the geocoder avoid any global name clashes
        place_query = f"{TARGET_CITY}, Israel"
        
        # network_type can be 'walk', 'drive', or 'all'
        G = ox.graph_from_place(place_query, network_type='walk')
        
        # Convert the graph to a GeoDataFrame of lines (streets)
        nodes, edges = ox.graph_to_gdfs(G)
        print(f"Successfully downloaded {len(edges)} street segments.")
        
    except Exception as e:
        print(f"Failed to fetch OpenStreetMap data for {place_query}: {e}")
        return

    # 4. Do the GIS Math
    print("Calculating exact intersection distances...")
    
    # Project both our Runs and the City Streets to a Web Mercator (meters) so we can do accurate distance math
    edges_m = edges.to_crs(CRS_METERS)
    runs_m = gdf_runs.to_crs(CRS_METERS)
    
    # Buffer our runs by 20 meters (so any street within 20 meters of the run path is "covered")
    try:
        runner_buffer = runs_m.geometry.buffer(BUFFER_RADIUS_METERS).union_all()
    except AttributeError:
        # Fallback for older Geopandas versions
        runner_buffer = runs_m.geometry.buffer(BUFFER_RADIUS_METERS).unary_union
        
    # Calculate unique total street length
    total_street_length_m = merged_line_length_m(edges_m.geometry)
    
    # Clip the street network down to ONLY the parts that fall inside the runner's buffer radius!
    covered_streets_m = gpd.clip(edges_m, runner_buffer)
    
    # Calculate difference to get only uncovered street parts
    uncovered_streets_m = edges_m.copy()
    uncovered_streets_m.geometry = uncovered_streets_m.geometry.difference(runner_buffer)
    uncovered_streets_m = uncovered_streets_m[~uncovered_streets_m.is_empty]
    
    # Calculate unique covered street length
    covered_street_length_m = merged_line_length_m(covered_streets_m.geometry)
    
    percentage_covered = (covered_street_length_m / total_street_length_m) * 100
    total_km = total_street_length_m / 1000
    unique_covered_km = covered_street_length_m / 1000
    total_ran_km = total_ran_distance_m / 1000
    
    print("\n" + "="*50)
    print(f"   STREET COVERAGE REPORT: {TARGET_CITY.upper()}")
    print("="*50)
    print(f" - Total Walkable Streets: {total_km:.2f} km")
    print(f" - Unique Distance Covered: {unique_covered_km:.2f} km")
    print(f" - Total Distance Physically Ran: {total_ran_km:.2f} km")
    print(f" - Completion Percentage:  {percentage_covered:.2f} %")
    print("="*50 + "\n")
    
    # 5. Build the HTML map Visualization
    print("Building local interactive HTML Map...")
    
    # Get center roughly based on first run
    center_lat, center_lon = city_run_paths[0][0]
    m = folium.Map(location=[center_lat, center_lon], zoom_start=13, tiles="CartoDB dark_matter")
    
    # Add UI Overlay for stats
    legend_html = f'''
     <div style="position: fixed; 
     bottom: 50px; left: 50px; width: 330px; height: 210px; 
     border:2px solid grey; z-index:9999; font-size:16px;
     background-color:rgba(30, 30, 30, 0.9);
     color: white; border-radius: 10px; padding: 15px; box-shadow: 2px 2px 10px rgba(0,0,0,0.5);">
     <h4 style="margin-top:0px; margin-bottom:10px;">{TARGET_CITY.upper()} Coverage</h4>
     <b>Total Walkable:</b> {total_km:.1f} km<br>
     <b style="color:#ff5555;">Unique Distance Covered:</b> {unique_covered_km:.1f} km<br>
     <b style="color:#d3b1ff;">Total Distance Ran:</b> {total_ran_km:.1f} km<br>
     <b style="color:#1f78b4;">Remaining Streets:</b> {(total_km - unique_covered_km):.1f} km<br>
     <hr style="border-color: #555;">
     <div style="font-size: 20px;"><b>Completion:</b> <span style="color: #4CAF50;">{percentage_covered:.2f}%</span></div>
     </div>
     '''
    m.get_root().html.add_child(folium.Element(legend_html))

    # Plot ONLY uncovered streets in a faint BLUE
    # Since Folium needs lat/lon coordinates, we convert back to EPSG:4326
    uncovered_streets_4326 = uncovered_streets_m.to_crs("EPSG:4326")
    
    folium.GeoJson(
        uncovered_streets_4326,
        style_function=lambda x: {'color': '#1f78b4', 'weight': 2, 'opacity': 0.6},
        name="Uncovered Streets (Blue)"
    ).add_to(m)
    
    # Plot the runs themselves in Bright Red/Orange
    # (By plotting our actual runs on top, it creates a very beautiful "painted over" effect)
    for path in city_run_paths:
        folium.PolyLine(
            path, # requires list of (lat, lon)
            weight=3,
            color="#ff5555",
            opacity=0.9,
            tooltip="Run Path"
        ).add_to(m)

    # Save and launch!
    map_file = os.path.join(os.getcwd(), f"{TARGET_CITY}_street_coverage.html")
    m.save(map_file)
    print(f"Created Map Visualization: {map_file}")
    
    import webbrowser
    webbrowser.open(map_file)

if __name__ == "__main__":
    main()
