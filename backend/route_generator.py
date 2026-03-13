import logging
import random

import networkx as nx
import osmnx as ox
import pyproj
from shapely.geometry import LineString, Point
from shapely.ops import substring, transform, unary_union

logger = logging.getLogger(__name__)

# Configure OSMNX
ox.settings.use_cache = True
ox.settings.log_console = False


class RouteGenerator:
    def __init__(self, buffer_radius=30):
        self.buffer_radius = buffer_radius

    @staticmethod
    def _edge_geometry(graph, u, v, k, edge_data=None):
        data = edge_data or graph.get_edge_data(u, v, k) or {}
        geometry = data.get("geometry")
        if geometry is None:
            u_data = graph.nodes[u]
            v_data = graph.nodes[v]
            geometry = LineString([(u_data["x"], u_data["y"]), (v_data["x"], v_data["y"])])
        return geometry

    @staticmethod
    def _path_length_m(graph, node_path):
        total = 0.0
        for u, v in zip(node_path[:-1], node_path[1:]):
            edge_data = graph.get_edge_data(u, v)
            if edge_data:
                total += min(d.get("length", 0) for d in edge_data.values())
        return total

    @staticmethod
    def _path_unvisited_ratio(graph, node_path):
        total = 0
        unvisited = 0
        for u, v in zip(node_path[:-1], node_path[1:]):
            edge_data = graph.get_edge_data(u, v)
            if not edge_data:
                continue
            total += 1
            best_edge = min(edge_data.values(), key=lambda d: d.get("length", float("inf")))
            if not best_edge.get("visited", True):
                unvisited += 1
        if total == 0:
            return 0.0
        return unvisited / total

    @staticmethod
    def _coords_close(coord_a, coord_b, tol=1e-8):
        return abs(coord_a[0] - coord_b[0]) < tol and abs(coord_a[1] - coord_b[1]) < tol

    @staticmethod
    def _sq_dist(p1, p2):
        return (p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2

    def _append_coords(self, route_coords, new_coords):
        for coord in new_coords:
            if not route_coords or not self._coords_close(route_coords[-1], coord):
                route_coords.append(coord)

    def _edge_path_coords_4326(self, graph, u, v, to_wgs84):
        edge_data = graph.get_edge_data(u, v)
        if not edge_data:
            return []

        best_k, best_edge = min(
            edge_data.items(),
            key=lambda item: item[1].get("length", float("inf")),
        )
        line = self._edge_geometry(graph, u, v, best_k, best_edge)

        u_xy = (graph.nodes[u]["x"], graph.nodes[u]["y"])
        v_xy = (graph.nodes[v]["x"], graph.nodes[v]["y"])
        line_coords = list(line.coords)
        if line_coords:
            forward = self._sq_dist(line_coords[0], u_xy) + self._sq_dist(line_coords[-1], v_xy)
            reverse = self._sq_dist(line_coords[0], v_xy) + self._sq_dist(line_coords[-1], u_xy)
            if reverse < forward:
                line_coords = list(reversed(line_coords))

        coords_4326 = []
        for x, y in line_coords:
            lon, lat = to_wgs84(x, y)
            coords_4326.append((lat, lon))
        return coords_4326

    def _partial_edge_coords_4326(self, line, from_point, to_point, to_wgs84):
        try:
            start_dist = line.project(from_point)
            end_dist = line.project(to_point)
            segment = substring(line, start_dist, end_dist)
            if segment.is_empty:
                return []
            if segment.geom_type == "MultiLineString":
                segment = max(segment.geoms, key=lambda g: g.length)

            coords_4326 = []
            for x, y in segment.coords:
                lon, lat = to_wgs84(x, y)
                coords_4326.append((lat, lon))
            return coords_4326
        except Exception:
            return []

    def _build_round_trip(self, graph, start_node, target_dist_m, lengths, unvisited_nodes):
        best_route = None
        best_score = float("inf")

        candidates = [
            node
            for node in unvisited_nodes
            if node in lengths and 0.2 * target_dist_m <= lengths[node] <= 0.55 * target_dist_m
        ]
        if not candidates:
            logger.info("No unvisited candidates in range, falling back to all nodes")
            candidates = [
                node
                for node, dist in lengths.items()
                if 0.2 * target_dist_m <= dist <= 0.55 * target_dist_m
            ]
        if not candidates:
            candidates = [node for node in lengths.keys() if node != start_node]

        random.shuffle(candidates)
        attempts = min(60, len(candidates))
        logger.info(f"Trying {attempts} candidates for round-trip midpoint...")

        for i in range(attempts):
            mid_node = candidates[i]
            try:
                path_to = nx.shortest_path(graph, start_node, mid_node, weight="weight")

                # Penalize outgoing path edges (both directions) so the return tends to diverge.
                original_weights = {}
                for u, v in zip(path_to[:-1], path_to[1:]):
                    for a, b in ((u, v), (v, u)):
                        edge_data = graph.get_edge_data(a, b)
                        if not edge_data:
                            continue
                        for k, edata in edge_data.items():
                            key = (a, b, k)
                            if key not in original_weights:
                                original_weights[key] = edata["weight"]
                                edata["weight"] *= 20

                try:
                    path_back = nx.shortest_path(graph, mid_node, start_node, weight="weight")
                finally:
                    for (u, v, k), weight in original_weights.items():
                        graph[u][v][k]["weight"] = weight

                full_path = path_to + path_back[1:]
                actual_length = self._path_length_m(graph, full_path)
                diff = abs(actual_length - target_dist_m)
                unvisited_ratio = self._path_unvisited_ratio(graph, full_path)
                score = diff - (0.04 * target_dist_m * unvisited_ratio)

                if score < best_score:
                    best_score = score
                    best_route = full_path

                if diff < 0.1 * target_dist_m and unvisited_ratio > 0.2:
                    logger.info(f"Found good round-trip route of length {actual_length:.2f}m")
                    break
            except Exception:
                continue

        return best_route

    def _build_one_way(self, graph, start_node, target_dist_m, lengths, unvisited_nodes):
        best_route = None
        best_score = float("inf")

        candidates = [
            node
            for node in unvisited_nodes
            if node in lengths and 0.7 * target_dist_m <= lengths[node] <= 1.3 * target_dist_m
        ]
        if not candidates:
            logger.info("No unvisited one-way candidates in range, falling back to all nodes")
            candidates = [
                node
                for node, dist in lengths.items()
                if 0.6 * target_dist_m <= dist <= 1.4 * target_dist_m
            ]
        if not candidates:
            candidates = [node for node in lengths.keys() if node != start_node]

        random.shuffle(candidates)
        attempts = min(80, len(candidates))
        logger.info(f"Trying {attempts} candidates for one-way endpoint...")

        for i in range(attempts):
            end_node = candidates[i]
            if end_node == start_node:
                continue
            try:
                full_path = nx.shortest_path(graph, start_node, end_node, weight="weight")
                actual_length = self._path_length_m(graph, full_path)
                diff = abs(actual_length - target_dist_m)
                unvisited_ratio = self._path_unvisited_ratio(graph, full_path)
                score = diff - (0.05 * target_dist_m * unvisited_ratio)

                if score < best_score:
                    best_score = score
                    best_route = full_path

                if diff < 0.08 * target_dist_m and unvisited_ratio > 0.25:
                    logger.info(f"Found good one-way route of length {actual_length:.2f}m")
                    break
            except Exception:
                continue

        return best_route

    def generate_route(self, city_name, start_point, target_distance_km, run_paths, trip_type="round_trip"):
        """
        start_point: [lat, lon]
        target_distance_km: float
        run_paths: list of lists of (lat, lon)
        trip_type: "round_trip" | "one_way"
        """
        logger.info(
            f"Generating route for {city_name} from {start_point} for "
            f"{target_distance_km}km ({trip_type})"
        )
        if trip_type not in {"round_trip", "one_way"}:
            logger.warning(f"Unknown trip_type '{trip_type}', falling back to round_trip")
            trip_type = "round_trip"

        # 1. Load and project graph
        place_query = f"{city_name}, Israel"
        if "Be'er Sheva" in city_name or "Be\u05d2\u20ac\u2122er-Sheva" in city_name:
            place_query = "Be'er Sheva, Israel"

        try:
            graph_4326 = ox.graph_from_place(place_query, network_type="walk")
            graph = ox.project_graph(graph_4326)
        except Exception as e:
            logger.error(f"Failed to load/project graph for {city_name}: {e}")
            return None

        # 2. Project clicked point and snap it to nearest road
        clicked_geom = Point(start_point[1], start_point[0])
        to_graph_crs = pyproj.Transformer.from_crs(
            "EPSG:4326", graph.graph["crs"], always_xy=True
        ).transform
        to_wgs84 = pyproj.Transformer.from_crs(
            graph.graph["crs"], "EPSG:4326", always_xy=True
        ).transform
        projected_click = transform(to_graph_crs, clicked_geom)

        snapped_point = None
        snapped_edge_geom = None
        try:
            edge_u, edge_v, edge_k = ox.distance.nearest_edges(
                graph, projected_click.x, projected_click.y
            )
            edge_geom = self._edge_geometry(graph, edge_u, edge_v, edge_k)
            snapped_point = edge_geom.interpolate(edge_geom.project(projected_click))
            snapped_edge_geom = edge_geom

            edge_u_point = Point(graph.nodes[edge_u]["x"], graph.nodes[edge_u]["y"])
            edge_v_point = Point(graph.nodes[edge_v]["x"], graph.nodes[edge_v]["y"])
            start_node = (
                edge_u
                if snapped_point.distance(edge_u_point) <= snapped_point.distance(edge_v_point)
                else edge_v
            )
        except Exception as e:
            logger.warning(f"Failed to snap to nearest edge, using nearest node fallback: {e}")
            start_node = ox.nearest_nodes(graph, projected_click.x, projected_click.y)
            node = graph.nodes[start_node]
            snapped_point = Point(node["x"], node["y"])

        # 3. Mark visited vs unvisited edges
        if run_paths:
            lines = [LineString([(lon, lat) for lat, lon in path]) for path in run_paths]
            runner_buffer_4326 = unary_union([line.buffer(0.0003) for line in lines])
            runner_buffer = transform(to_graph_crs, runner_buffer_4326)
        else:
            runner_buffer = None

        unvisited_nodes = []
        for u, v, k, data in graph.edges(data=True, keys=True):
            length = data.get("length", 0)
            edge_geom = self._edge_geometry(graph, u, v, k, data)

            is_visited = False
            if runner_buffer and edge_geom.intersects(runner_buffer):
                is_visited = True

            data["visited"] = is_visited
            # Strongly prefer unvisited edges.
            data["weight"] = length * (50 if is_visited else 1)

        for node, _ in graph.nodes(data=True):
            has_unvisited = False
            for _, _, _, edge_data in graph.edges(node, data=True, keys=True):
                if not edge_data.get("visited", True):
                    has_unvisited = True
                    break
            if has_unvisited:
                unvisited_nodes.append(node)

        # 4. Build route
        target_dist_m = target_distance_km * 1000
        try:
            lengths = nx.single_source_dijkstra_path_length(
                graph, start_node, weight="length"
            )
        except Exception as e:
            logger.error(f"Failed to calculate dijkstra path lengths: {e}")
            return None

        if trip_type == "one_way":
            best_route = self._build_one_way(
                graph, start_node, target_dist_m, lengths, unvisited_nodes
            )
        else:
            best_route = self._build_round_trip(
                graph, start_node, target_dist_m, lengths, unvisited_nodes
            )

        if not best_route:
            logger.warning("No route found after all attempts")
            return None

        # 4b. Calculate new (unvisited) distance contributed by this route
        new_distance_m = 0.0
        for u, v in zip(best_route[:-1], best_route[1:]):
            edge_data = graph.get_edge_data(u, v)
            if edge_data:
                best_edge = min(edge_data.values(), key=lambda d: d.get("length", float("inf")))
                if not best_edge.get("visited", True):
                    new_distance_m += best_edge.get("length", 0)
        new_distance_km = new_distance_m / 1000

        # 5. Convert to coordinates (lat, lon), starting at snapped road point.
        snapped_lon, snapped_lat = to_wgs84(snapped_point.x, snapped_point.y)
        snapped_start = (snapped_lat, snapped_lon)

        route_coords = [snapped_start]
        if snapped_edge_geom is not None:
            start_node_point = Point(graph.nodes[start_node]["x"], graph.nodes[start_node]["y"])
            connector = self._partial_edge_coords_4326(
                snapped_edge_geom,
                snapped_point,
                start_node_point,
                to_wgs84,
            )
            self._append_coords(route_coords, connector)

        for u, v in zip(best_route[:-1], best_route[1:]):
            edge_coords = self._edge_path_coords_4326(graph, u, v, to_wgs84)
            self._append_coords(route_coords, edge_coords)

        if trip_type == "round_trip":
            if snapped_edge_geom is not None:
                start_node_point = Point(graph.nodes[start_node]["x"], graph.nodes[start_node]["y"])
                connector_back = self._partial_edge_coords_4326(
                    snapped_edge_geom,
                    start_node_point,
                    snapped_point,
                    to_wgs84,
                )
                self._append_coords(route_coords, connector_back)
            if not self._coords_close(route_coords[-1], snapped_start):
                route_coords.append(snapped_start)

        logger.info(f"Returning route with {len(route_coords)} coordinates, new_distance_km={new_distance_km:.2f}")
        return route_coords, new_distance_km
