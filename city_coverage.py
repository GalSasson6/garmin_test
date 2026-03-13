import os
import json
import logging
import geopandas as gpd
from shapely.geometry import LineString
from shapely.ops import linemerge, unary_union
import folium
import osmnx as ox
import warnings
import webbrowser

# Suppress the union_all warnings
warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CACHE_DIR = ".garmin_cache"
POLYLINES_DIR = os.path.join(CACHE_DIR, "polylines")
CRS_METERS = "EPSG:3857" # Meter-based projection
BUFFER_RADIUS_METERS = 30 # How close to a street you need to be to "cover" it

def merged_line_length_m(geometries):
    """
    Compute unique line length by dissolving and merging overlaps.
    This avoids double counting from duplicated graph directions/segments.
    """
    valid_lines = [geom for geom in geometries if geom is not None and not geom.is_empty]
    if not valid_lines:
        return 0.0

    try:
        dissolved = unary_union(valid_lines)
    except Exception:
        # Fallback for older shapely or complex geometries
        dissolved = unary_union([g.buffer(0.01) for g in valid_lines])
        
    if dissolved.geom_type == 'MultiLineString' or dissolved.geom_type == 'LineString':
        merged = linemerge(dissolved)
        return merged.length
    elif hasattr(dissolved, 'geoms'):
        # If it's a GeometryCollection or something else, sum the lengths of linear parts
        return sum(g.length for g in dissolved.geoms if g.geom_type in ['LineString', 'MultiLineString'])
    else:
        return dissolved.length

def is_in_israel(lat, lon):
    """Simple bounding box check for Israel."""
    return 29.4 <= lat <= 33.5 and 34.2 <= lon <= 35.9

def main():
    print("Preparing to analyze street coverage for cities in Israel...")
    
    # 1. Load the run history and cached cities
    runs_file = os.path.join(CACHE_DIR, "runs_summary.json")
    if not os.path.exists(runs_file):
        print("Runs cache not found. Please run the previous map script first.")
        return
        
    with open(runs_file, "r") as f:
        all_activities = json.load(f)
        
    runs = [act for act in all_activities if act.get('activityType', {}).get('typeKey', '') in ['running']]
    
    city_cache_file = os.path.join(CACHE_DIR, "city_cache.json")
    if os.path.exists(city_cache_file):
        with open(city_cache_file, "r", encoding="utf-8") as f:
            city_cache = json.load(f)
    else:
        city_cache = {}

    # Group runs by city
    cities_data = {}
    
    for run in runs:
        act_id = str(run['activityId'])
        poly_file = os.path.join(POLYLINES_DIR, f"{act_id}.json")
        
        if os.path.exists(poly_file):
            with open(poly_file, "r") as f:
                polyline = json.load(f)
                
            if len(polyline) > 1:
                start_lat, start_lon = polyline[0]
                
                # ONLY process if the run is in Israel
                if not is_in_israel(start_lat, start_lon):
                    continue

                coords_key = f"{round(start_lat, 3)},{round(start_lon, 3)}"
                
                # Check what city this run is in
                city_name = city_cache.get(coords_key, "Unknown")
                if city_name == "Unknown":
                    continue
                
                if city_name not in cities_data:
                    cities_data[city_name] = {
                        'run_paths': [],
                        'total_ran_distance_m': 0,
                        'center': [start_lat, start_lon]
                    }
                
                cities_data[city_name]['run_paths'].append(polyline)
                cities_data[city_name]['total_ran_distance_m'] += run.get('distance', 0) or 0

    if not cities_data:
        print("No runs with known city locations found in cache.")
        return
        
    print(f"Found runs in {len(cities_data)} cities: {', '.join(cities_data.keys())}")
    
    # Initialize Map
    first_city = list(cities_data.values())[0]
    m = folium.Map(location=first_city['center'], zoom_start=12, tiles="CartoDB dark_matter")
    
    all_stats = []
    
    for city_name, data in cities_data.items():
        print(f"\n--- Processing {city_name} ---")
        city_run_paths = data['run_paths']
        total_ran_distance_m = data['total_ran_distance_m']
        
        # Convert runs into Shapely Geometries
        shapely_lines = [LineString([(lon, lat) for lat, lon in path]) for path in city_run_paths]
        gdf_runs = gpd.GeoDataFrame(geometry=shapely_lines, crs="EPSG:4326")
        
        # 3. Fetch the walkable street network for the city
        # Fix common mojibake/encoding issues for OSM lookup
        display_name = city_name
        if "Be\u05d2\u20ac\u2122er-Sheva" in city_name or "Be\u05d2" in city_name:
            place_query = "Be'er Sheva, Israel"
            display_name = "Be'er Sheva"
        else:
            place_query = city_name
            if city_name in ["Raanana", "Beit HaAm"]:
                place_query = f"{city_name}, Israel"
        
        print(f"Downloading OpenStreetMap network for '{place_query}'...")
        try:
            G = ox.graph_from_place(place_query, network_type='walk')
            nodes, edges = ox.graph_to_gdfs(G)
            print(f"Successfully downloaded {len(edges)} street segments.")
        except Exception as e:
            print(f"Failed to fetch OpenStreetMap data for {place_query}: {e}")
            # Try without "Israel" if it failed
            if ", Israel" in place_query:
                try:
                    place_query = city_name
                    print(f"Retrying with '{place_query}'...")
                    G = ox.graph_from_place(place_query, network_type='walk')
                    nodes, edges = ox.graph_to_gdfs(G)
                    print(f"Successfully downloaded {len(edges)} street segments.")
                except Exception as e2:
                    print(f"Retry failed: {e2}")
                    continue
            else:
                continue

        # 4. GIS Math
        edges_m = edges.to_crs(CRS_METERS)
        runs_m = gdf_runs.to_crs(CRS_METERS)
        
        try:
            runner_buffer = runs_m.geometry.buffer(BUFFER_RADIUS_METERS).union_all()
        except AttributeError:
            runner_buffer = runs_m.geometry.buffer(BUFFER_RADIUS_METERS).unary_union
            
        total_street_length_m = merged_line_length_m(edges_m.geometry)
        
        # Clip/Difference to find coverage
        uncovered_streets_m = edges_m.copy()
        uncovered_streets_m.geometry = uncovered_streets_m.geometry.difference(runner_buffer)
        uncovered_streets_m = uncovered_streets_m[~uncovered_streets_m.is_empty]
        
        covered_streets_m = gpd.clip(edges_m, runner_buffer)
        covered_street_length_m = merged_line_length_m(covered_streets_m.geometry)
        
        percentage_covered = (covered_street_length_m / total_street_length_m) * 100 if total_street_length_m > 0 else 0
        total_km = total_street_length_m / 1000
        unique_covered_km = covered_street_length_m / 1000
        total_ran_km = total_ran_distance_m / 1000
        
        stats = {
            'city': city_name,
            'total_km': total_km,
            'covered_km': unique_covered_km,
            'ran_km': total_ran_km,
            'percent': percentage_covered
        }
        all_stats.append(stats)
        
        # 5. Add to Map
        city_group = folium.FeatureGroup(name=f"{city_name} Coverage")
        
        # Plot uncovered streets in BLUE
        uncovered_streets_4326 = uncovered_streets_m.to_crs("EPSG:4326")
        folium.GeoJson(
            uncovered_streets_4326,
            style_function=lambda x: {'color': '#1f78b4', 'weight': 2, 'opacity': 0.6},
            tooltip=f"Uncovered Street in {city_name}"
        ).add_to(city_group)
        
        # Plot runs in RED
        for path in city_run_paths:
            folium.PolyLine(
                path,
                weight=3,
                color="#ff5555",
                opacity=0.8,
                tooltip=f"Run Path in {city_name}"
            ).add_to(city_group)
            
        city_group.add_to(m)

    # Build the HTML summary Overlay
    rows_html = ""
    for s in sorted(all_stats, key=lambda x: x['percent'], reverse=True):
        rows_html += f"""
        <tr>
            <td style="padding: 5px;">{s['city']}</td>
            <td style="padding: 5px; text-align: right;">{s['total_km']:.1f} km</td>
            <td style="padding: 5px; text-align: right; color: #4CAF50;">{s['percent']:.1f}%</td>
        </tr>
        """

    legend_html = f'''
     <div style="position: fixed; 
     bottom: 50px; left: 50px; width: 350px; max-height: 400px; 
     border:2px solid grey; z-index:9999; font-size:14px;
     background-color:rgba(30, 30, 30, 0.9);
     color: white; border-radius: 10px; padding: 15px; box-shadow: 2px 2px 10px rgba(0,0,0,0.5);
     overflow-y: auto;">
     <h4 style="margin-top:0px; margin-bottom:10px;">City Coverage Summary</h4>
     <table style="width: 100%; border-collapse: collapse;">
        <thead>
            <tr style="border-bottom: 1px solid #555;">
                <th style="text-align: left; padding: 5px;">City</th>
                <th style="text-align: right; padding: 5px;">Total Streets</th>
                <th style="text-align: right; padding: 5px;">Completion</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
     </table>
     <hr style="border-color: #555;">
     <div style="font-size: 12px; color: #aaa;">
        <span style="color: #ff5555;">■</span> Your Runs | 
        <span style="color: #1f78b4;">■</span> Uncovered Streets
     </div>
     </div>
     '''
    m.get_root().html.add_child(folium.Element(legend_html))
    folium.LayerControl().add_to(m)

    # Save and launch
    map_file = os.path.join(os.getcwd(), "all_cities_coverage.html")
    m.save(map_file)
    print(f"\nCreated Map Visualization: {map_file}")
    
    webbrowser.open(map_file)

if __name__ == "__main__":
    main()
