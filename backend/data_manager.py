import os
import json
import logging
import time
import hashlib
from datetime import datetime
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
from shapely.ops import linemerge, unary_union
import osmnx as ox
from geopy.geocoders import Nominatim
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectAuthenticationError,
)

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure OSMNX
ox.settings.use_cache = True
ox.settings.log_console = False

CACHE_DIR = ".garmin_cache"
POLYLINES_DIR = os.path.join(CACHE_DIR, "polylines")
STATS_CACHE_FILE = os.path.join(CACHE_DIR, "backend_city_stats_cache.json")
CRS_METERS = "EPSG:3857"
COVERAGE_BUFFER_RADIUS_METERS = 12
UNIQUE_TRACE_BUFFER_RADIUS_METERS = 12
STATS_CACHE_VERSION = 3

class DataManager:
    def __init__(self):
        self.email = os.getenv("GARMIN_EMAIL")
        self.password = os.getenv("GARMIN_PASSWORD")
        self.tokenstore = os.getenv("GARMINTOKENS") or "~/.garminconnect"
        self.api = None
        
        os.makedirs(POLYLINES_DIR, exist_ok=True)
        
        self.runs_summary = self._load_runs_summary()
        self.city_cache = self._load_city_cache()
        self.stats_cache = self._load_stats_cache()
        self.is_authenticated = False

    def _load_runs_summary(self):
        runs_file = os.path.join(CACHE_DIR, "runs_summary.json")
        if os.path.exists(runs_file):
            with open(runs_file, "r") as f:
                return json.load(f)
        return []

    def _load_city_cache(self):
        city_cache_file = os.path.join(CACHE_DIR, "city_cache.json")
        if os.path.exists(city_cache_file):
            with open(city_cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _load_stats_cache(self):
        if os.path.exists(STATS_CACHE_FILE):
            try:
                with open(STATS_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_stats_cache(self):
        with open(STATS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.stats_cache, f, ensure_ascii=False)

    def _get_stats_cache_key(self, city_name, run_ids):
        # Include the stats algorithm version so stale/bad cached values are not reused.
        ids_str = ",".join(sorted([str(rid) for rid in run_ids]))
        key_payload = (
            f"v{STATS_CACHE_VERSION}|coverage_buffer={COVERAGE_BUFFER_RADIUS_METERS}|"
            f"trace_buffer={UNIQUE_TRACE_BUFFER_RADIUS_METERS}|{city_name}|{ids_str}"
        )
        hash_val = hashlib.md5(key_payload.encode()).hexdigest()
        return f"{city_name}_{hash_val}"

    def _iter_linear_parts(self, geometry):
        if geometry is None or geometry.is_empty:
            return

        geom_type = geometry.geom_type
        if geom_type == "LineString":
            yield geometry
            return
        if geom_type == "MultiLineString":
            for part in geometry.geoms:
                if part is not None and not part.is_empty:
                    yield part
            return
        if hasattr(geometry, "geoms"):
            for part in geometry.geoms:
                yield from self._iter_linear_parts(part)

    def _linearize_gdf(self, gdf, extra_columns=None):
        extra_columns = extra_columns or []
        records = []

        for _, row in gdf.iterrows():
            base = {col: row[col] for col in extra_columns if col in row}
            for part in self._iter_linear_parts(row.geometry):
                record = dict(base)
                record["geometry"] = part
                records.append(record)

        columns = list(extra_columns) + ["geometry"]
        if not records:
            return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs=gdf.crs)

        return gpd.GeoDataFrame(records, geometry="geometry", crs=gdf.crs)

    def _cap_unique_covered_length_m(self, covered_street_length_m, total_ran_distance_m):
        if covered_street_length_m <= total_ran_distance_m:
            return covered_street_length_m

        logger.warning(
            "Unique covered length exceeded total running distance "
            f"({covered_street_length_m:.2f}m > {total_ran_distance_m:.2f}m); capping."
        )
        return total_ran_distance_m

    def _approx_unique_trace_length_m(self, geometries, buffer_radius_m):
        buffered_geometries = [
            geom.buffer(buffer_radius_m)
            for geom in geometries
            if geom is not None and not geom.is_empty
        ]
        if not buffered_geometries:
            return 0.0

        buffered_series = gpd.GeoSeries(buffered_geometries, crs=CRS_METERS)
        try:
            union_geom = buffered_series.union_all()
        except AttributeError:
            union_geom = buffered_series.unary_union

        # Approximate the centerline length of the unioned run corridor.
        return union_geom.area / (2 * buffer_radius_m)

    def authenticate(self, mfa_code=None):
        """
        Authenticate with Garmin Connect.
        If mfa_code is provided, use it for MFA.
        Otherwise, try using cached tokens.
        """
        def get_mfa():
            if mfa_code:
                return mfa_code
            # This is tricky for a web backend. We'll need to handle MFA flow via API.
            # For now, we'll assume tokens are present or MFA is not required.
            raise Exception("MFA required")

        self.api = Garmin(self.email, self.password, prompt_mfa=get_mfa)
        try:
            self.api.login(self.tokenstore)
            self.is_authenticated = True
            return True, "Authenticated"
        except (FileNotFoundError, GarminConnectAuthenticationError):
            try:
                # If mfa_code is not provided, this will fail if MFA is needed
                self.api.login()
                self.api.garth.dump(os.path.expanduser(self.tokenstore))
                self.is_authenticated = True
                return True, "Authenticated and tokens dumped"
            except Exception as e:
                logger.error(f"Authentication Failed: {e}")
                return False, str(e)
        except Exception as e:
            logger.error(f"A general error occurred: {e}")
            return False, str(e)

    def fetch_new_activities(self):
        if not self.is_authenticated:
            success, msg = self.authenticate()
            if not success:
                return False, msg

        all_activities = []
        for i in range(0, 2000, 500):
            chunk = self.api.get_activities(i, 500)
            if not chunk:
                break
            all_activities.extend(chunk)
            if len(chunk) < 500:
                break
            time.sleep(1)
        
        runs_file = os.path.join(CACHE_DIR, "runs_summary.json")
        with open(runs_file, "w") as f:
            json.dump(all_activities, f)
        
        self.runs_summary = all_activities
        return True, f"Fetched {len(all_activities)} activities"

    def get_city_name(self, lat, lon):
        coords_key = f"{round(lat, 3)},{round(lon, 3)}"
        if coords_key in self.city_cache:
            return self.city_cache[coords_key]
            
        try:
            geolocator = Nominatim(user_agent="garmin_city_mapper", timeout=10)
            location = geolocator.reverse(f"{lat}, {lon}", language="en")
            address = location.raw.get("address", {})
            city = address.get("city") or address.get("town") or address.get("village") or address.get("municipality")
            if not city:
                 city = location.address.split(",")[0]
            
            # Normalize city name
            if city:
                city = city.strip()
            
            self.city_cache[coords_key] = city
            city_cache_file = os.path.join(CACHE_DIR, "city_cache.json")
            with open(city_cache_file, "w", encoding="utf-8") as f:
                json.dump(self.city_cache, f, ensure_ascii=False)
                
            time.sleep(1) # respect Nominatim ratelimits
            return city
        except Exception as e:
            logger.warning(f"Failed to geocode {lat}, {lon}: {e}")
            return "Unknown"

    def get_polyline(self, activity_id):
        act_id = str(activity_id)
        poly_file = os.path.join(POLYLINES_DIR, f"{act_id}.json")
        
        if os.path.exists(poly_file):
            with open(poly_file, "r") as f:
                return json.load(f)
        
        if not self.is_authenticated:
            success, msg = self.authenticate()
            if not success:
                return []

        try:
            details = self.api.get_activity_details(act_id)
            poly_data = details.get("geoPolylineDTO", {}).get("polyline", [])
            polyline = [(pt["lat"], pt["lon"]) for pt in poly_data]
            
            with open(poly_file, "w") as f:
                json.dump(polyline, f)
            time.sleep(1) 
            return polyline
        except Exception as e:
            logger.error(f"Failed to get details for {act_id}: {e}")
            return []

    def get_cities(self):
        runs = [act for act in self.runs_summary if act.get('activityType', {}).get('typeKey', '') in ['running']]
        cities = set()
        for run in runs:
            act_id = str(run['activityId'])
            poly_file = os.path.join(POLYLINES_DIR, f"{act_id}.json")
            if os.path.exists(poly_file):
                with open(poly_file, "r") as f:
                    polyline = json.load(f)
                    if polyline:
                        lat, lon = polyline[0]
                        city = self.get_city_name(lat, lon)
                        if city and city != "Unknown":
                            cities.add(city)
        return sorted(list(cities))

    def get_city_stats(self, city_name):
        # Wrapper for synchronous calls that just returns the final result
        generator = self.get_city_stats_stream(city_name)
        final_result = None
        for update in generator:
            if update.get('type') == 'result':
                final_result = update['result']
        return final_result

    def get_city_stats_stream(self, city_name):
        yield {"status": "Filtering activities...", "progress": 10, "type": "progress"}
        
        runs = [act for act in self.runs_summary if act.get('activityType', {}).get('typeKey', '') in ['running']]
        city_run_paths = []
        city_run_ids = []
        total_ran_distance_m = 0
        
        # Quick filtering of runs in this city
        for run in runs:
            act_id = str(run['activityId'])
            poly_file = os.path.join(POLYLINES_DIR, f"{act_id}.json")
            if os.path.exists(poly_file):
                with open(poly_file, "r") as f:
                    polyline = json.load(f)
                    if polyline:
                        lat, lon = polyline[0]
                        if self.get_city_name(lat, lon) == city_name:
                            city_run_paths.append(polyline)
                            city_run_ids.append(act_id)
                            total_ran_distance_m += run.get('distance', 0) or 0

        if not city_run_paths:
            yield {"status": "No runs found for this city.", "progress": 100, "type": "progress"}
            yield {"result": None, "type": "result"}
            return

        # Check Cache
        yield {"status": "Checking cache...", "progress": 20, "type": "progress"}
        cache_key = self._get_stats_cache_key(city_name, city_run_ids)
        if cache_key in self.stats_cache:
            logger.info(f"Returning cached stats for {city_name}")
            yield {"status": "Loading from cache...", "progress": 100, "type": "progress"}
            yield {"result": self.stats_cache[cache_key], "type": "result"}
            return

        logger.info(f"Calculating new stats for {city_name}")

        # GIS calculations
        shapely_lines = [LineString([(lon, lat) for lat, lon in path]) for path in city_run_paths]
        gdf_runs = gpd.GeoDataFrame(geometry=shapely_lines, crs="EPSG:4326")
        runs_m = gdf_runs.to_crs(CRS_METERS)
        
        # City boundary and street network (OSMNX)
        place_query = f"{city_name}, Israel"
        if "Be'er Sheva" in city_name or "Be\u05d2\u20ac\u2122er-Sheva" in city_name:
             place_query = "Be'er Sheva, Israel"

        yield {"status": f"Downloading {city_name} street network (OSM)...", "progress": 40, "type": "progress"}
        try:
            city_gdf = ox.geocode_to_gdf(place_query)
            city_gdf_m = city_gdf.to_crs(CRS_METERS)
            city_area_sq_m = city_gdf_m.geometry.unary_union.area
            city_area_sq_km = city_area_sq_m / 1_000_000
        except:
            city_area_sq_km = 0

        uncovered_streets_coords = []
        try:
            G = ox.graph_from_place(place_query, network_type='walk')
            nodes, edges = ox.graph_to_gdfs(G)
            edges_m = edges.to_crs(CRS_METERS)
            total_street_length_m = self.merged_line_length_m(edges_m.geometry)
            
            yield {"status": "Calculating street coverage and counts...", "progress": 80, "type": "progress"}
            # Create buffers for each individual run
            run_buffers = [geom.buffer(COVERAGE_BUFFER_RADIUS_METERS) for geom in runs_m.geometry]
            gdf_buffers = gpd.GeoDataFrame(geometry=run_buffers, crs=CRS_METERS)
            gdf_buffers['run_index'] = range(len(run_buffers))
            
            # Use spatial join to count how many runs intersect each edge
            edges_with_runs = gpd.sjoin(edges_m, gdf_buffers, how='inner', predicate='intersects')
            
            # Count unique runs per edge. The join result has a multi-index if edges had one.
            # We want to count how many distinct 'run_index' values matched each original edge.
            # Easiest way: group by the original index of edges_m.
            run_counts = edges_with_runs.groupby(edges_with_runs.index).run_index.nunique()
            
            # Map counts back to edges_m
            edges_m['pass_count'] = run_counts
            
            # Only count the actual street segments inside the run buffer,
            # not the full edge length for every touched edge.
            try:
                combined_buffer = gdf_buffers.geometry.union_all()
            except AttributeError:
                combined_buffer = gdf_buffers.geometry.unary_union

            covered_edges = edges_m[edges_m['pass_count'] > 0].copy()
            covered_edges.geometry = covered_edges.geometry.intersection(combined_buffer)
            covered_edges = self._linearize_gdf(covered_edges, extra_columns=['pass_count'])
            
            covered_street_length_m = self.merged_line_length_m(covered_edges.geometry)
            approx_unique_trace_length_m = self._approx_unique_trace_length_m(
                runs_m.geometry,
                UNIQUE_TRACE_BUFFER_RADIUS_METERS,
            )
            if covered_street_length_m > approx_unique_trace_length_m:
                logger.info(
                    "Clipping unique covered length to approximate unique trace length "
                    f"({covered_street_length_m:.2f}m -> {approx_unique_trace_length_m:.2f}m)"
                )
                covered_street_length_m = approx_unique_trace_length_m
            covered_street_length_m = self._cap_unique_covered_length_m(
                covered_street_length_m,
                total_ran_distance_m,
            )
            
            # Format covered streets for the map (with pass counts)
            covered_streets_data = []
            covered_edges_4326 = covered_edges.to_crs("EPSG:4326")
            for idx, row in covered_edges_4326.iterrows():
                geom = row.geometry
                try:
                    count = int(row['pass_count'])
                except (ValueError, TypeError):
                    count = 1
                if geom.geom_type == 'LineString':
                    covered_streets_data.append({
                        'path': [(lat, lon) for lon, lat in geom.coords],
                        'count': count
                    })
                elif geom.geom_type == 'MultiLineString':
                    for part in geom.geoms:
                        covered_streets_data.append({
                            'path': [(lat, lon) for lon, lat in part.coords],
                            'count': count
                        })

            # Uncovered streets for mapping
            uncovered_streets_gdf = edges_m.copy()
            uncovered_streets_gdf.geometry = uncovered_streets_gdf.geometry.difference(combined_buffer)
            uncovered_streets_gdf = self._linearize_gdf(uncovered_streets_gdf)
            
            yield {"status": "Formatting map data...", "progress": 95, "type": "progress"}
            # Convert uncovered streets back to 4326 for the map
            uncovered_streets_4326 = uncovered_streets_gdf.to_crs("EPSG:4326")
            for geom in uncovered_streets_4326.geometry:
                if geom.geom_type == 'LineString':
                    uncovered_streets_coords.append([(lat, lon) for lon, lat in geom.coords])
                elif geom.geom_type == 'MultiLineString':
                    for part in geom.geoms:
                        uncovered_streets_coords.append([(lat, lon) for lon, lat in part.coords])
            
            unique_covered_km = covered_street_length_m / 1000
            total_street_km = total_street_length_m / 1000
            percent_coverage = (covered_street_length_m / total_street_length_m) * 100 if total_street_length_m > 0 else 0
        except Exception as e:
            logger.error(f"Error fetching OSM data for {city_name}: {e}")
            unique_covered_km = 0
            total_street_km = 0
            percent_coverage = 0
            covered_streets_data = []

        stats_result = {
            'city': city_name,
            'total_ran_km': total_ran_distance_m / 1000,
            'unique_covered_km': unique_covered_km,
            'total_street_km': total_street_km,
            'city_area_sq_km': city_area_sq_km,
            'percent_coverage': percent_coverage,
            'run_paths': city_run_paths, # Keep raw paths for legacy/internal use if needed
            'covered_streets': covered_streets_data, # New grouped data
            'uncovered_streets': uncovered_streets_coords
        }

        # Save to cache
        self.stats_cache[cache_key] = stats_result
        self._save_stats_cache()

        yield {"status": "Complete!", "progress": 100, "type": "progress"}
        yield {"result": stats_result, "type": "result"}


    def merged_line_length_m(self, geometries):
        valid_lines = []
        for geom in geometries:
            valid_lines.extend(list(self._iter_linear_parts(geom)))
        if not valid_lines:
            return 0.0
        try:
            dissolved = unary_union(valid_lines)
        except Exception:
            dissolved = unary_union([g.buffer(0.01) for g in valid_lines])
            
        if dissolved.geom_type == 'MultiLineString' or dissolved.geom_type == 'LineString':
            merged = linemerge(dissolved)
            return merged.length
        elif hasattr(dissolved, 'geoms'):
            return sum(g.length for g in dissolved.geoms if g.geom_type in ['LineString', 'MultiLineString'])
        else:
            return dissolved.length

