import unittest

import geopandas as gpd
from shapely.geometry import LineString

from backend.data_manager import DataManager


class CoverageLogicTest(unittest.TestCase):
    def setUp(self):
        self.data_manager = DataManager()

    def test_unique_covered_is_capped_by_total_running_distance(self):
        run_line = LineString([(0, 0), (100, 0)])
        edges = gpd.GeoDataFrame(
            {"pass_count": [1, 1]},
            geometry=[
                run_line,
                LineString([(50, -50), (50, 50)]),
            ],
            crs="EPSG:3857",
        )

        covered_edges = edges.copy()
        covered_edges.geometry = covered_edges.geometry.intersection(run_line.buffer(30))
        covered_edges = self.data_manager._linearize_gdf(
            covered_edges,
            extra_columns=["pass_count"],
        )

        clipped_covered_length_m = self.data_manager.merged_line_length_m(
            covered_edges.geometry
        )

        self.assertGreater(clipped_covered_length_m, 100.0)
        self.assertEqual(
            self.data_manager._cap_unique_covered_length_m(
                clipped_covered_length_m,
                total_ran_distance_m=100.0,
            ),
            100.0,
        )

    def test_unique_trace_estimate_collapses_repeated_routes(self):
        repeated_route = LineString([(0, 0), (1000, 0)])
        approx_unique_length_m = self.data_manager._approx_unique_trace_length_m(
            [repeated_route, repeated_route],
            buffer_radius_m=12,
        )

        self.assertGreaterEqual(approx_unique_length_m, 1000.0)
        self.assertLess(approx_unique_length_m, 1025.0)

    def test_unique_coverage_metrics_do_not_double_count_repeated_routes(self):
        repeated_route = LineString([(0, 0), (1000, 0)])
        crossing_street = LineString([(500, -60), (500, 60)])
        segments = gpd.GeoDataFrame(
            {"pass_count": [2, 2, 1]},
            geometry=[
                repeated_route,
                repeated_route,
                crossing_street,
            ],
            crs="EPSG:3857",
        )

        unique_covered_length_m, covered_segments, _ = (
            self.data_manager._calculate_unique_covered_length_m(
                segments,
                [repeated_route, repeated_route],
                total_ran_distance_m=2000.0,
            )
        )

        self.assertGreaterEqual(unique_covered_length_m, 1000.0)
        self.assertLess(unique_covered_length_m, 1025.0)
        self.assertLess(unique_covered_length_m, 2000.0)
        self.assertGreaterEqual(len(covered_segments), 1)


if __name__ == "__main__":
    unittest.main()
