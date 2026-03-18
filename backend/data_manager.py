import os
import json
import logging
import time
import hashlib
import sqlite3
from datetime import datetime
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, Point
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
DATABASE_FILE = os.path.join(CACHE_DIR, "garmin_data.gpkg")
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
        self._init_db()

    def _init_db(self):
        # We use GeoPackage via geopandas. 
        # Tables/Layers we need:
        # 1. 'cities': name, total_street_km, city_area_sq_km, last_updated
        # 2. 'processed_runs': activity_id, city_name, processed_at
        # 3. 'segments_{city_hash}': geometry, is_covered, pass_count
        pass

    def _get_city_layer_name(self, city_name):
        # GeoPackage layer names should be simple
        clean_name = "".join(c for c in city_name if c.isalnum())
        return f"segments_{clean_name}"

    def _load_layer(self, layer_name):
        if not os.path.exists(DATABASE_FILE):
            return None
        try:
            return gpd.read_file(DATABASE_FILE, layer=layer_name, engine="pyogrio")
        except:
            return None

    def _save_layer(self, gdf, layer_name):
        gdf.to_file(DATABASE_FILE, layer=layer_name, driver="GPKG", engine="pyogrio")

    def _get_processed_runs(self):
        df = self._load_layer("processed_runs")
        if df is None:
            return pd.DataFrame(columns=["activity_id", "city_name", "processed_at"])
        return pd.DataFrame(df.drop(columns="geometry"))

    def _mark_runs_processed(self, activity_ids, city_name):
        runs = self._get_processed_runs()
        new_records = []
        now = datetime.now().isoformat()
        for act_id in activity_ids:
            new_records.append({
                "activity_id": str(act_id),
                "city_name": city_name,
                "processed_at": now
            })
        
        if not new_records:
            return

        updated_runs = pd.concat([runs, pd.DataFrame(new_records)], ignore_index=True)
        # GeoPackage needs a geometry column even for metadata tables if using geopandas
        gdf = gpd.GeoDataFrame(updated_runs, geometry=[Point(0,0)]*len(updated_runs), crs="EPSG:4326")
        self._save_layer(gdf, "processed_runs")

    def _init_city_segments(self, city_name, place_query):
        """Initial download and segmenting of a city's street network."""
        logger.info(f"Initializing segments for {city_name}")
        G = ox.graph_from_place(place_query, network_type='walk')
        nodes, edges = ox.graph_to_gdfs(G)
        edges_m = edges.to_crs(CRS_METERS)
        
        # Linearize to ensure we have simple LineStrings
        segments_gdf = self._linearize_gdf(edges_m)
        segments_gdf['is_covered'] = 0
        segments_gdf['pass_count'] = 0
        
        layer_name = self._get_city_layer_name(city_name)
        self._save_layer(segments_gdf, layer_name)
        
        # Store metadata including the "True" unique street length
        total_unique_length_m = self.merged_line_length_m(segments_gdf.geometry)
        cities_df = self._load_layer("cities")
        if cities_df is None:
            cities_df = pd.DataFrame(columns=["name", "total_unique_length_m", "city_area_sq_km", "last_updated"])
        
        new_city_meta = {
            "name": city_name,
            "total_unique_length_m": total_unique_length_m,
            "city_area_sq_km": 0, # Will be updated later
            "last_updated": datetime.now().isoformat()
        }
        
        # Update or add
        if city_name in cities_df['name'].values:
            cities_df.loc[cities_df['name'] == city_name, ["total_unique_length_m", "last_updated"]] = [total_unique_length_m, new_city_meta["last_updated"]]
        else:
            cities_df = pd.concat([cities_df, pd.DataFrame([new_city_meta])], ignore_index=True)
            
        cities_gdf = gpd.GeoDataFrame(cities_df, geometry=[Point(0,0)]*len(cities_df), crs="EPSG:4326")
        self._save_layer(cities_gdf, "cities")
        
        return segments_gdf

    def _update_city_stats_incremental(self, city_name, run_ids, city_run_paths, place_query):
        layer_name = self._get_city_layer_name(city_name)
        segments_gdf = self._load_layer(layer_name)
        
        if segments_gdf is None:
            segments_gdf = self._init_city_segments(city_name, place_query)
        
        processed_runs = self._get_processed_runs()
        processed_ids = set(processed_runs[processed_runs['city_name'] == city_name]['activity_id'].astype(str).tolist())
        
        new_run_ids_to_mark = []
        new_runs_count = 0
        
        for act_id, path in zip(run_ids, city_run_paths):
            if str(act_id) in processed_ids:
                continue
                
            logger.info(f"Processing run {act_id} for {city_name} incrementally")
            run_line = LineString([(lon, lat) for lat, lon in path])
            run_gdf = gpd.GeoDataFrame(geometry=[run_line], crs="EPSG:4326").to_crs(CRS_METERS)
            run_buffer = run_gdf.geometry.iloc[0].buffer(COVERAGE_BUFFER_RADIUS_METERS)
            
            # Find intersecting segments
            intersects = segments_gdf.intersects(run_buffer)
            segments_gdf.loc[intersects, 'is_covered'] = 1
            segments_gdf.loc[intersects, 'pass_count'] += 1
            
            new_run_ids_to_mark.append(act_id)
            new_runs_count += 1
            
        if new_runs_count > 0:
            self._save_layer(segments_gdf, layer_name)
            self._mark_runs_processed(new_run_ids_to_mark, city_name)
            
        return segments_gdf

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

    def _empty_line_gdf(self, crs, extra_columns=None):
        columns = list(extra_columns or []) + ["geometry"]
        return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs=crs)

    def _polyline_length_m(self, polyline):
        if not polyline or len(polyline) < 2:
            return 0.0

        run_line = LineString([(lon, lat) for lat, lon in polyline])
        run_gdf = gpd.GeoDataFrame(geometry=[run_line], crs="EPSG:4326").to_crs(CRS_METERS)
        return float(run_gdf.geometry.length.iloc[0])

    def _build_run_corridor(self, geometries, buffer_radius_m):
        buffered_geometries = [
            geom.buffer(buffer_radius_m)
            for geom in geometries
            if geom is not None and not geom.is_empty
        ]
        if not buffered_geometries:
            return None

        buffered_series = gpd.GeoSeries(buffered_geometries, crs=CRS_METERS)
        try:
            return buffered_series.union_all()
        except AttributeError:
            return buffered_series.unary_union

    def _clip_segments_to_geometry(self, segments_gdf, clip_geometry, extra_columns=None):
        crs = segments_gdf.crs if segments_gdf is not None else CRS_METERS
        if segments_gdf is None or segments_gdf.empty or clip_geometry is None or clip_geometry.is_empty:
            return self._empty_line_gdf(crs, extra_columns=extra_columns)

        clipped_segments = segments_gdf.copy()
        clipped_segments.geometry = clipped_segments.geometry.intersection(clip_geometry)
        clipped_segments = self._linearize_gdf(clipped_segments, extra_columns=extra_columns)
        if clipped_segments.empty:
            return clipped_segments

        return clipped_segments[~clipped_segments.geometry.is_empty].reset_index(drop=True)

    def _difference_segments_from_geometry(self, segments_gdf, erase_geometry):
        crs = segments_gdf.crs if segments_gdf is not None else CRS_METERS
        if segments_gdf is None or segments_gdf.empty:
            return self._empty_line_gdf(crs)

        remaining_segments = segments_gdf.copy()
        if erase_geometry is None or erase_geometry.is_empty:
            remaining_segments = self._linearize_gdf(remaining_segments)
        else:
            remaining_segments.geometry = remaining_segments.geometry.difference(erase_geometry)
            remaining_segments = self._linearize_gdf(remaining_segments)

        if remaining_segments.empty:
            return remaining_segments

        return remaining_segments[~remaining_segments.geometry.is_empty].reset_index(drop=True)

    def _calculate_unique_covered_length_m(self, segments_gdf, run_geometries, total_ran_distance_m):
        extra_columns = [col for col in ["pass_count"] if col in segments_gdf.columns]
        run_corridor = self._build_run_corridor(run_geometries, COVERAGE_BUFFER_RADIUS_METERS)
        if run_corridor is None or run_corridor.is_empty:
            return 0.0, self._empty_line_gdf(segments_gdf.crs, extra_columns=extra_columns), None

        touched_segments = segments_gdf[segments_gdf.intersects(run_corridor)].copy()
        covered_segments = self._clip_segments_to_geometry(
            touched_segments,
            run_corridor,
            extra_columns=extra_columns,
        )

        if "pass_count" in covered_segments.columns and not covered_segments.empty:
            covered_segments["pass_count"] = (
                pd.to_numeric(covered_segments["pass_count"], errors="coerce")
                .fillna(0)
                .astype(int)
                .clip(lower=1)
            )

        clipped_covered_length_m = self.merged_line_length_m(covered_segments.geometry)
        approx_unique_trace_length_m = self._approx_unique_trace_length_m(
            run_geometries,
            UNIQUE_TRACE_BUFFER_RADIUS_METERS,
        )

        if (
            approx_unique_trace_length_m > 0
            and clipped_covered_length_m > approx_unique_trace_length_m
        ):
            logger.info(
                "Clipping unique covered length to approximate unique trace length "
                f"({clipped_covered_length_m:.2f}m -> {approx_unique_trace_length_m:.2f}m)"
            )
            clipped_covered_length_m = approx_unique_trace_length_m

        unique_covered_length_m = self._cap_unique_covered_length_m(
            clipped_covered_length_m,
            total_ran_distance_m,
        )
        return unique_covered_length_m, covered_segments, run_corridor

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

        previous_ids = {
            str(act.get("activityId"))
            for act in self.runs_summary
            if act.get("activityId") is not None
        }

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
        new_running_activities = [
            act
            for act in self._get_running_activities(all_activities)
            if str(act.get("activityId")) not in previous_ids
        ]
        prefetched_count = self._prefetch_recent_polylines(new_running_activities)
        return (
            True,
            f"Fetched {len(all_activities)} activities and cached {prefetched_count} "
            f"new run polylines",
        )

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
            try:
                with open(poly_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read cached polyline for {act_id}: {e}")
        
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

    def _get_running_activities(self, activities=None):
        source = activities if activities is not None else self.runs_summary
        return [
            act
            for act in source
            if act.get("activityType", {}).get("typeKey", "") == "running"
        ]

    def _get_run_polyline(self, run):
        if not run.get("hasPolyline", True):
            return []
        return self.get_polyline(run["activityId"])

    def _prefetch_recent_polylines(self, activities, limit=10):
        if not activities:
            return 0

        recent_activities = sorted(
            activities,
            key=lambda act: act.get("startTimeLocal", ""),
            reverse=True,
        )[:limit]

        prefetched_count = 0
        for run in recent_activities:
            act_id = str(run["activityId"])
            poly_file = os.path.join(POLYLINES_DIR, f"{act_id}.json")
            if os.path.exists(poly_file):
                continue
            if self._get_run_polyline(run):
                prefetched_count += 1

        return prefetched_count

    def get_cities(self):
        runs = self._get_running_activities()
        cities = set()
        for run in runs:
            polyline = self._get_run_polyline(run)
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
        # 1. Load the run history and cached cities
        runs_file = os.path.join(CACHE_DIR, "runs_summary.json")
        if not os.path.exists(runs_file):
            logger.error("Runs cache not found.")
            yield {"status": "Error: Runs cache not found.", "progress": 100, "type": "progress"}
            yield {"result": None, "type": "result"}
            return
            
        with open(runs_file, "r") as f:
            all_activities = json.load(f)
        
        runs = self._get_running_activities(all_activities)
        city_run_paths = []
        city_run_ids = []
        total_ran_distance_m = 0
        
        # Quick filtering of runs in this city
        city_runs_metadata = []
        for run in runs:
            act_id = str(run['activityId'])
            polyline = self._get_run_polyline(run)
            if polyline:
                lat, lon = polyline[0]
                if self.get_city_name(lat, lon) == city_name:
                    city_run_paths.append(polyline)
                    city_run_ids.append(act_id)
                    city_runs_metadata.append(run)
                    distance_m = float(run.get('distance', 0) or 0)
                    if distance_m <= 0:
                        distance_m = self._polyline_length_m(polyline)
                    total_ran_distance_m += distance_m

        if not city_run_paths:
            yield {"status": "No runs found for this city.", "progress": 100, "type": "progress"}
            yield {"result": None, "type": "result"}
            return
            
        # Sort metadata by date to find the last run
        # Garmin dates are usually "2023-10-27 08:30:15"
        city_runs_metadata.sort(key=lambda x: x.get('startTimeLocal', ''), reverse=True)
        last_run_meta = city_runs_metadata[0]
        last_run_path = self._get_run_polyline(last_run_meta)

        last_run_data = {
            'date': last_run_meta.get('startTimeLocal', 'Unknown'),
            'distance_km': (last_run_meta.get('distance', 0) or 0) / 1000,
            'duration_mins': (last_run_meta.get('duration', 0) or 0) / 60,
            'speed_kmh': (last_run_meta.get('averageSpeed', 0) or 0) * 3.6, # m/s to km/h
            'path': last_run_path
        }

        # City boundary and street network query
        place_query = f"{city_name}, Israel"
        if "Be'er Sheva" in city_name or "Be\u05d2\u20ac\u2122er-Sheva" in city_name:
             place_query = "Be'er Sheva, Israel"

        yield {"status": f"Syncing {city_name} coverage database...", "progress": 40, "type": "progress"}
        try:
            segments_gdf = self._update_city_stats_incremental(city_name, city_run_ids, city_run_paths, place_query)
        except Exception as e:
            logger.error(f"Error updating DB for {city_name}: {e}")
            yield {"status": f"Error: {str(e)}", "progress": 100, "type": "progress"}
            yield {"result": None, "type": "result"}
            return

        yield {"status": "Calculating statistics...", "progress": 80, "type": "progress"}

        # NEW: Use the database flags instead of re-calculating the union of all runs
        covered_mask = segments_gdf['is_covered'] > 0
        covered_segments = segments_gdf[covered_mask].copy()
        uncovered_segments = segments_gdf[~covered_mask].copy()
        
        # 1. Get total unique city length from cache
        cities_df = self._load_layer("cities")
        total_street_length_m = 0
        if cities_df is not None and city_name in cities_df['name'].values:
            total_street_length_m = cities_df.loc[cities_df['name'] == city_name, "total_unique_length_m"].values[0]
        
        if total_street_length_m <= 0:
            # Fallback if metadata is missing (should not happen with new _init_city_segments)
            total_street_length_m = self.merged_line_length_m(segments_gdf.geometry)

        # 2. Get unique covered length using merging (only on covered parts, which is faster)
        covered_street_length_m = self.merged_line_length_m(covered_segments.geometry)
        
        # Safety: Cap covered length if it somehow exceeds total
        if covered_street_length_m > total_street_length_m:
            covered_street_length_m = total_street_length_m
            
        # City area calculation
        try:
            city_gdf = ox.geocode_to_gdf(place_query)
            city_area_sq_km = city_gdf.to_crs(CRS_METERS).geometry.unary_union.area / 1_000_000
        except:
            city_area_sq_km = 0

        # Format covered streets for the map
        covered_streets_data = []
        if not covered_segments.empty:
            covered_4326 = covered_segments.to_crs("EPSG:4326")
            for idx, row in covered_4326.iterrows():
                geom = row.geometry
                count = int(row.get('pass_count', 1) or 1)
                if geom.geom_type == 'LineString':
                    covered_streets_data.append({
                        'path': [(lat, lon) for lon, lat in geom.coords],
                        'count': count
                    })

        # Uncovered streets for mapping
        uncovered_streets_coords = []
        if not uncovered_segments.empty:
            uncovered_4326 = uncovered_segments.to_crs("EPSG:4326")
            for geom in uncovered_4326.geometry:
                if geom.geom_type == 'LineString':
                    uncovered_streets_coords.append([(lat, lon) for lon, lat in geom.coords])

        percent_coverage = (covered_street_length_m / total_street_length_m) * 100 if total_street_length_m > 0 else 0

        
        stats_result = {
            'city': city_name,
            'total_ran_km': total_ran_distance_m / 1000,
            'unique_covered_km': covered_street_length_m / 1000,
            'total_street_km': total_street_length_m / 1000,
            'city_area_sq_km': city_area_sq_km,
            'percent_coverage': percent_coverage,
            'run_paths': city_run_paths,
            'covered_streets': covered_streets_data,
            'uncovered_streets': uncovered_streets_coords,
            'last_run': last_run_data
        }

        yield {"status": "Complete!", "progress": 100, "type": "progress"}
        yield {"result": stats_result, "type": "result"}


    def get_all_runs(self):
        runs = self._get_running_activities()
        all_paths = []
        total_ran_distance_m = 0
        
        for run in runs:
            polyline = self._get_run_polyline(run)
            if polyline:
                all_paths.append(polyline)
                distance_m = float(run.get('distance', 0) or 0)
                if distance_m <= 0:
                    distance_m = self._polyline_length_m(polyline)
                total_ran_distance_m += distance_m

        return {
            'city': 'All Runs',
            'total_ran_km': total_ran_distance_m / 1000,
            'unique_covered_km': 0,
            'total_street_km': 0,
            'city_area_sq_km': 0,
            'percent_coverage': 0,
            'run_paths': all_paths,
            'covered_streets': [],
            'uncovered_streets': []
        }

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

