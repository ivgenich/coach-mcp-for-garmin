"""get_course_profile: elevation profile and climbs of a Garmin Connect course.

Upstream's get_course_details reports a course's geo point count but none of
the points, so grades can't be worked out from it. This tool reads the same
course, finds its climbs on the server, and can add an elevation profile
thinned to about 500 points.

A course has no timing, so climbs are delimited by distance, unlike the
time-based detection get_activity_fit_data runs on recorded rides. Grade uses
the same 100 m window and 3% threshold, so the numbers line up with it.

The analysis functions are pure (standard library only) so the tests run
without garmin_mcp installed; launch.py wires register_tools into upstream.

The course name is typed in Garmin, so it is cleaned where it is read
(text_hygiene.name_or_none). The tool catches nothing: a Garmin error, and
NoData for a course without elevation, reach launch.py's tool-error wrapper,
which answers each with a fixed text.
"""

import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import status_common as sc
import text_hygiene as th

# Resample the course onto a fixed distance grid before taking grades.
_GRID_M = 20.0
# Grade over a window centred on each grid point, as in get_activity_fit_data.
_GRADE_WINDOW_M = 100.0
_GRADE_CLAMP_PCT = 40.0
# A climb ends once grade stays below the threshold for longer than this.
_FLAT_TOLERANCE_M = 300.0
# Default profile size, and the cap when the caller asks for a finer step.
_PROFILE_TARGET_POINTS = 500
_PROFILE_MAX_POINTS = 2000

_EARTH_RADIUS_M = 6371000.0


def _haversine_m(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    lat1, lon1 = math.radians(a["latitude"]), math.radians(a["longitude"])
    lat2, lon2 = math.radians(b["latitude"]), math.radians(b["longitude"])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _distance_elevation(geo_points: Sequence[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """(cumulative distance m, elevation m) per point, distance strictly increasing.

    Uses the points' own cumulative "distance" when every point has one, else
    sums great-circle distances, each measured from the last point that had
    coordinates (a point without them is skipped, not a break in the route).
    Points without elevation, and points that don't move the distance
    forward, are dropped.
    """
    use_own = bool(geo_points) and all(p.get("distance") is not None for p in geo_points)
    out: List[Tuple[float, float]] = []
    dist = 0.0
    prev = None  # the last point with coordinates
    for p in geo_points:
        if use_own:
            dist = float(p["distance"])
        elif p.get("latitude") is not None and p.get("longitude") is not None:
            if prev is not None:
                dist += _haversine_m(prev, p)
            prev = p
        ele = p.get("elevation")
        if ele is None:
            continue
        if out and dist <= out[-1][0]:
            continue
        out.append((dist, float(ele)))
    return out


def _resample(points: List[Tuple[float, float]], step_m: float) -> List[Tuple[float, float]]:
    """Linearly interpolate elevation every step_m from the first point to the last."""
    if not points:
        return []
    start, end = points[0][0], points[-1][0]
    n = int((end - start) // step_m)
    out = []
    j = 0
    for k in range(n + 1):
        d = start + k * step_m
        while j + 1 < len(points) - 1 and points[j + 1][0] < d:
            j += 1
        (d0, e0), (d1, e1) = points[j], points[min(j + 1, len(points) - 1)]
        e = e0 if d1 == d0 else e0 + (e1 - e0) * (d - d0) / (d1 - d0)
        out.append((d, e))
    if out[-1][0] < end:
        out.append(points[-1])
    return out


def _grades(grid: List[Tuple[float, float]]) -> List[float]:
    """Grade % at each grid point over a centred _GRADE_WINDOW_M window."""
    half = max(1, int(round(_GRADE_WINDOW_M / 2 / _GRID_M)))
    out = []
    for i in range(len(grid)):
        lo, hi = max(0, i - half), min(len(grid) - 1, i + half)
        span = grid[hi][0] - grid[lo][0]
        g = (grid[hi][1] - grid[lo][1]) / span * 100 if span > 0 else 0.0
        out.append(max(-_GRADE_CLAMP_PCT, min(_GRADE_CLAMP_PCT, g)))
    return out


def _summarize_climb(grid, grades, start: int, end: int) -> Dict[str, Any]:
    length = grid[end][0] - grid[start][0]
    gain = grid[end][1] - grid[start][1]
    ascent = sum(max(0.0, grid[i + 1][1] - grid[i][1]) for i in range(start, end))
    return {
        "start_km": round(grid[start][0] / 1000, 2),
        "end_km": round(grid[end][0] / 1000, 2),
        "length_m": round(length),
        "start_elevation_m": round(grid[start][1]),
        "end_elevation_m": round(grid[end][1]),
        "elevation_gain_m": round(gain),
        "total_ascent_m": round(ascent),
        "avg_grade_pct": round(gain / length * 100, 1) if length > 0 else None,
        "max_grade_pct": round(max(grades[start:end + 1]), 1),
    }


def detect_climbs(
    grid: List[Tuple[float, float]],
    min_grade_pct: float = 3.0,
    min_gain_m: float = 30.0,
) -> List[Dict[str, Any]]:
    """Climbs on an evenly spaced (distance, elevation) grid.

    A climb opens where grade reaches min_grade_pct and closes once grade has
    stayed below it for more than _FLAT_TOLERANCE_M; shorter dips stay inside.
    Each end then moves to the lowest (start) or highest (end) point within
    one grade window, since the centred window triggers a little late and
    releases a little early. Climbs gaining less than min_gain_m are dropped.
    """
    if len(grid) < 2:
        return []
    grades = _grades(grid)
    step = grid[1][0] - grid[0][0]
    window = max(1, int(round(_GRADE_WINDOW_M / step)))
    tolerance = int(round(_FLAT_TOLERANCE_M / step))

    spans = []
    start = None
    flat = 0
    for i, g in enumerate(grades):
        if start is None:
            if g >= min_grade_pct:
                start, flat = i, 0
        elif g < min_grade_pct:
            flat += 1
            if flat > tolerance:
                spans.append((start, i - flat))
                start = None
        else:
            flat = 0
    if start is not None:
        spans.append((start, len(grid) - 1 - flat))

    climbs = []
    for s, e in spans:
        # Scan outward from the climb so a tie on flat ground keeps the
        # point nearest the climb, not the far end of the window.
        lo = max(0, s - window)
        s = min(range(s, lo - 1, -1), key=lambda k: grid[k][1])
        hi = min(len(grid) - 1, e + window)
        e = max(range(e, hi + 1), key=lambda k: grid[k][1])
        if e <= s:
            continue
        climb = _summarize_climb(grid, grades, s, e)
        if climb["elevation_gain_m"] >= min_gain_m:
            climbs.append(climb)
    return climbs


def build_profile(points: List[Tuple[float, float]], step_m: int = 0) -> Dict[str, Any]:
    """[km, elevation m] pairs every step_m (0 = auto, about 500 points)."""
    total = points[-1][0] - points[0][0] if points else 0.0
    floor_step = math.ceil(total / _PROFILE_MAX_POINTS) if total else 1
    if step_m and step_m > 0:
        step = max(int(step_m), floor_step)
    else:
        step = max(100, int(math.ceil(total / _PROFILE_TARGET_POINTS / 10) * 10))
    samples = _resample(points, float(step))
    return {
        "step_m": step,
        "columns": ["distance_km", "elevation_m"],
        "points": [[round(d / 1000, 2), round(e)] for d, e in samples],
    }


def analyze_course(
    data: Dict[str, Any],
    include_profile: bool = True,
    step_m: int = 0,
    min_grade_pct: float = 3.0,
    min_gain_m: float = 30.0,
    activity_names: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """The tool's result for one /course-service/course/{id} response.

    Raises status_common.NoData (a ValueError) when fewer than two points
    carry an elevation.
    """
    geo_points = data.get("geoPoints") or []
    points = _distance_elevation(geo_points)
    if len(points) < 2:
        raise sc.NoData("course has fewer than two points with elevation")

    grid = _resample(points, _GRID_M)
    climbs = detect_climbs(grid, min_grade_pct=min_grade_pct, min_gain_m=min_gain_m)
    elevations = [e for _, e in points]
    type_pk = data.get("activityTypePk")

    result: Dict[str, Any] = {
        "course_id": data.get("courseId"),
        "name": th.name_or_none(data.get("courseName")),
        "activity": (activity_names or {}).get(type_pk, type_pk),
        "distance_m": round(points[-1][0] - points[0][0]),
        "elevation_gain_m": _round_or_none(data.get("elevationGainMeter")),
        "elevation_loss_m": _round_or_none(data.get("elevationLossMeter")),
        "min_elevation_m": round(min(elevations)),
        "max_elevation_m": round(max(elevations)),
        "geo_points_count": len(geo_points),
        "climb_rule": (
            f"starts at >= {min_grade_pct:g}% grade over {_GRADE_WINDOW_M:g} m, "
            f"ends after {_FLAT_TOLERANCE_M:g} m below it, kept if it gains >= {min_gain_m:g} m; "
            "max_grade_pct is the steepest 100 m; total_ascent_m includes dips' recovery"
        ),
        "climbs_count": len(climbs),
        "climbs": climbs,
    }
    if include_profile:
        result["profile"] = build_profile(points, step_m)
    return result


def _round_or_none(value):
    return round(value) if isinstance(value, (int, float)) else None


def register_tools(app, get_client, activity_names=None):
    """Register get_course_profile on upstream's (filtered) app.

    get_client returns upstream's configured Garmin client at call time.
    """

    @app.tool()
    async def get_course_profile(
        course_id: int,
        include_profile: bool = True,
        step_m: int = 0,
        min_grade_pct: float = 3.0,
        min_gain_m: float = 30.0,
    ) -> str:
        """Elevation profile and climbs of a Garmin Connect course, for pacing.

        Returns course totals plus every climb (start/end km, length,
        elevation gain, average grade, steepest 100 m grade). With
        include_profile, adds [distance_km, elevation_m] pairs: about 500
        points by default (one every ~160 m on an 80 km course, ~3k tokens).
        Every number is computed on the server: report the numbers as given
        and do not recompute them.

        Args:
            course_id: ID of the course (from get_courses).
            include_profile: Add the thinned elevation profile. Set false for
                climbs only.
            step_m: Profile spacing in metres; 0 picks one for ~500 points.
                Capped at 2000 points.
            min_grade_pct: Grade at which a climb starts (default 3).
            min_gain_m: Smallest elevation gain reported as a climb (default 30).
        """
        # No try: a Garmin error and NoData propagate to launch.py's tool-error wrapper.
        data = get_client().client.connectapi(f"/course-service/course/{course_id}")
        if not isinstance(data, dict) or not data.get("geoPoints"):
            return f"Error: course {course_id} not found or missing geoPoints."
        result = analyze_course(
            data,
            include_profile=include_profile,
            step_m=step_m,
            min_grade_pct=min_grade_pct,
            min_gain_m=min_gain_m,
            activity_names=activity_names,
        )
        # Compact: indent=2 would put each profile number on its own line.
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return app
