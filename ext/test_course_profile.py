"""Tests for course_profile. Standard library only:

    python3 -m unittest discover -s ext -v
"""

import json
import random
import unittest

import course_profile as cp


def course(segments, step_m=10.0, start_ele=100.0, noise_m=0.0, seed=1):
    """Synthetic course response from (length m, grade %) segments."""
    rng = random.Random(seed)
    points, dist, ele = [], 0.0, start_ele
    points.append({"latitude": 31.0, "longitude": -100.0, "distance": 0.0, "elevation": ele})
    for length, grade in segments:
        for _ in range(int(length / step_m)):
            dist += step_m
            ele += step_m * grade / 100
            points.append({
                "latitude": 31.0, "longitude": -100.0 + dist / 95000,
                "distance": dist, "elevation": ele + rng.uniform(-noise_m, noise_m),
            })
    return {"courseId": 1, "courseName": "t", "activityTypePk": 10, "geoPoints": points}


def climbs_of(data, **kw):
    return cp.analyze_course(data, include_profile=False, **kw)["climbs"]


class DetectClimbs(unittest.TestCase):
    def test_flat_course_has_no_climbs(self):
        self.assertEqual(climbs_of(course([(10000, 0)])), [])

    def test_single_climb_measured(self):
        (c,) = climbs_of(course([(2000, 0), (3000, 6), (2000, 0)]))
        self.assertAlmostEqual(c["start_km"], 2.0, delta=0.06)
        self.assertAlmostEqual(c["end_km"], 5.0, delta=0.06)
        self.assertAlmostEqual(c["elevation_gain_m"], 180, delta=4)
        self.assertAlmostEqual(c["avg_grade_pct"], 6.0, delta=0.2)
        self.assertAlmostEqual(c["max_grade_pct"], 6.0, delta=0.2)

    def test_short_dip_stays_inside_one_climb(self):
        (c,) = climbs_of(course([(1000, 0), (1500, 5), (200, -2), (1500, 5), (1000, 0)]))
        self.assertAlmostEqual(c["length_m"], 3200, delta=60)
        self.assertGreater(c["total_ascent_m"], c["elevation_gain_m"])

    def test_long_flat_splits_two_climbs(self):
        cs = climbs_of(course([(1000, 0), (1000, 5), (800, 0), (1000, 8), (1000, 0)]))
        self.assertEqual(len(cs), 2)
        self.assertAlmostEqual(cs[0]["avg_grade_pct"], 5.0, delta=0.3)
        self.assertAlmostEqual(cs[1]["avg_grade_pct"], 8.0, delta=0.3)

    def test_max_grade_finds_steep_ramp(self):
        (c,) = climbs_of(course([(1000, 0), (1000, 4), (200, 12), (1000, 4), (1000, 0)]))
        self.assertGreater(c["max_grade_pct"], 10.5)

    def test_small_climb_below_min_gain_dropped(self):
        data = course([(1000, 0), (400, 5), (1000, 0)])  # 20 m gain
        self.assertEqual(climbs_of(data), [])
        self.assertEqual(len(climbs_of(data, min_gain_m=15)), 1)

    def test_gentle_drag_below_threshold_ignored(self):
        self.assertEqual(climbs_of(course([(5000, 2)])), [])
        self.assertEqual(len(climbs_of(course([(5000, 2)]), min_grade_pct=1.5)), 1)

    def test_climb_running_to_course_end(self):
        (c,) = climbs_of(course([(1000, 0), (2000, 7)]))
        self.assertAlmostEqual(c["end_km"], 3.0, delta=0.03)

    def test_noisy_elevation_still_one_climb(self):
        data = course([(2000, 0), (3000, 6), (2000, 0)], noise_m=1.0)
        (c,) = climbs_of(data)
        self.assertAlmostEqual(c["elevation_gain_m"], 180, delta=6)


class Points(unittest.TestCase):
    def test_missing_elevation_and_backward_distance_dropped(self):
        pts = [
            {"distance": 0, "elevation": 1}, {"distance": 10, "elevation": None},
            {"distance": 20, "elevation": 3}, {"distance": 15, "elevation": 9},
            {"distance": 30, "elevation": 4},
        ]
        self.assertEqual(cp._distance_elevation(pts), [(0.0, 1.0), (20.0, 3.0), (30.0, 4.0)])

    def test_haversine_fallback_without_distance(self):
        pts = [{"latitude": 31.0, "longitude": -100.0 + i * 0.001, "elevation": 0} for i in range(3)]
        out = cp._distance_elevation(pts)
        self.assertAlmostEqual(out[-1][0], 190.6, delta=1)  # 2 x 0.001 deg at 31 N

    def test_too_few_points_raises(self):
        with self.assertRaises(ValueError):
            cp.analyze_course({"geoPoints": [{"distance": 0, "elevation": 1}]})


class Profile(unittest.TestCase):
    def test_auto_step_about_500_points(self):
        data = course([(80000, 0)], step_m=12)
        prof = cp.analyze_course(data)["profile"]
        self.assertEqual(prof["step_m"], 160)
        self.assertTrue(480 <= len(prof["points"]) <= 510)
        self.assertEqual(prof["points"][0], [0.0, 100])

    def test_short_course_uses_100m_floor(self):
        prof = cp.analyze_course(course([(5000, 0)]))["profile"]
        self.assertEqual(prof["step_m"], 100)

    def test_requested_step_capped_at_2000_points(self):
        prof = cp.analyze_course(course([(80000, 0)], step_m=20), step_m=5)["profile"]
        self.assertEqual(prof["step_m"], 40)
        self.assertLessEqual(len(prof["points"]), 2001)

    def test_profile_interpolates_elevation(self):
        prof = cp.analyze_course(course([(1000, 10)]), step_m=250)["profile"]
        self.assertEqual(prof["points"], [[0.0, 100], [0.25, 125], [0.5, 150], [0.75, 175], [1.0, 200]])

    def test_include_profile_false_omits_it(self):
        self.assertNotIn("profile", cp.analyze_course(course([(1000, 0)]), include_profile=False))


class Tool(unittest.TestCase):
    """The registered tool, against a fake app and client."""

    def setUp(self):
        self.tools = {}
        test = self

        class App:
            def tool(self):
                def deco(fn):
                    test.tools[fn.__name__] = fn
                    return fn
                return deco

        class Client:
            response = course([(1000, 0), (2000, 6), (1000, 0)])

            def connectapi(self, path):
                test.path = path
                return self.response

        self.client = type("Proxy", (), {"client": Client()})()
        cp.register_tools(App(), lambda: self.client, {10: "road_biking"})

    def call(self, **kw):
        import asyncio
        return asyncio.run(self.tools["get_course_profile"](**kw))

    def test_returns_compact_json(self):
        out = self.call(course_id=7)
        self.assertEqual(self.path, "/course-service/course/7")
        self.assertNotIn("\n", out)
        res = json.loads(out)
        self.assertEqual(res["activity"], "road_biking")
        self.assertEqual(res["climbs_count"], 1)
        self.assertIn("profile", res)

    def test_missing_course_returns_error_text(self):
        self.client.client.response = {}
        self.assertTrue(self.call(course_id=7).startswith("Error: course 7 not found"))


if __name__ == "__main__":
    unittest.main()
