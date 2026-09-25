"""GPX parsing, clipping by cover circles, and GPX re-export.

Only the Python standard library (xml.etree) is used for GPX handling.
All geometry is computed on a local equirectangular projection in metres,
which is accurate enough for point-to-line intersection at hiking scales.
"""
from __future__ import annotations

import math
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

R = 6_371_000.0  # mean Earth radius in metres


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Point:
    lon: float
    lat: float
    ele: Optional[float]
    time: Optional[datetime]
    seg_index: int = 0
    index: int = 0

    def to_dict(self) -> dict:
        return {
            "lon": self.lon,
            "lat": self.lat,
            "ele": self.ele,
            "time": iso_time(self.time),
        }


@dataclass
class CoverCircle:
    lon: float
    lat: float
    radius: float  # metres


@dataclass
class ProjectedTrack:
    segments: list[list[Point]]
    proj: "LocalProjection"
    coords: list[list[tuple[float, float]]]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def parse_time(text: str) -> Optional[datetime]:
    """Parse an ISO-8601 / RFC-3339 time string; return an aware UTC datetime."""
    text = text.strip()
    if not text:
        return None
    try:
        # Python <3.11 cannot parse the trailing 'Z'.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_time(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# GPX parsing
# --------------------------------------------------------------------------- #
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_gpx(data: bytes) -> list[list[Point]]:
    """Parse a GPX document into a list of segments of ordered points."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"无法解析 GPX 文件（XML 不合法）: {exc}") from exc

    segments: list[list[Point]] = []
    for trk in root.iter():
        if _localname(trk.tag) != "trk":
            continue
        for seg in trk:
            if _localname(seg.tag) != "trkseg":
                continue
            pts: list[Point] = []
            for node in seg:
                if _localname(node.tag) != "trkpt":
                    continue
                try:
                    lon = float(node.attrib["lon"])
                    lat = float(node.attrib["lat"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                    continue
                ele: Optional[float] = None
                tm: Optional[datetime] = None
                for child in node:
                    name = _localname(child.tag)
                    if name == "ele" and child.text:
                        try:
                            ele = float(child.text.strip())
                        except ValueError:
                            ele = None
                    elif name == "time" and child.text:
                        tm = parse_time(child.text)
                pts.append(Point(lon=lon, lat=lat, ele=ele, time=tm))
            if len(pts) >= 2:
                segments.append(pts)

    # Drop exact duplicate consecutive points that would otherwise produce
    # zero-length segments during clipping.
    cleaned: list[list[Point]] = []
    for seg in segments:
        out: list[Point] = []
        for p in seg:
            if out and p.lon == out[-1].lon and p.lat == out[-1].lat:
                continue
            out.append(p)
        if len(out) >= 2:
            cleaned.append(out)

    if not cleaned:
        raise ValueError("GPX 中没有找到包含至少两个有效轨迹点的 trkseg。")
    return cleaned


# --------------------------------------------------------------------------- #
# Projection and distance
# --------------------------------------------------------------------------- #
class LocalProjection:
    """Equirectangular projection centred on the track bounds (metres)."""

    def __init__(self, points: list[Point]):
        lons = [p.lon for p in points]
        lats = [p.lat for p in points]
        self.min_lon, self.max_lon = min(lons), max(lons)
        self.min_lat, self.max_lat = min(lats), max(lats)
        self.lon0 = (self.min_lon + self.max_lon) / 2.0
        self.lat0 = (self.min_lat + self.max_lat) / 2.0
        self.kx = math.radians(1.0) * R * math.cos(math.radians(self.lat0))
        self.ky = math.radians(1.0) * R

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def unproject(self, x: float, y: float) -> tuple[float, float]:
        return (x / self.kx + self.lon0, y / self.ky + self.lat0)

    @property
    def bounds_m(self) -> tuple[float, float, float, float]:
        xmin, ymin = self.project(self.min_lon, self.min_lat)
        xmax, ymax = self.project(self.max_lon, self.max_lat)
        return xmin, ymin, xmax, ymax


def haversine_m(p1: Point, p2: Point) -> float:
    lat1, lat2 = math.radians(p1.lat), math.radians(p2.lat)
    dlat = math.radians(p2.lat - p1.lat)
    dlon = math.radians(p2.lon - p1.lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def track_distance_m(segments: list[list[Point]]) -> float:
    total = 0.0
    for seg in segments:
        for a, b in zip(seg, seg[1:]):
            total += haversine_m(a, b)
    return total


def interpolate(a: Point, b: Point, t: float, proj: LocalProjection,
                seg_index: int) -> Point:
    """Point at fraction t along a->b. Position is interpolated in metres,
    elevation/time linearly (time proportional to arc-length parameter)."""
    xa, ya = proj.project(a.lon, a.lat)
    xb, yb = proj.project(b.lon, b.lat)
    lon, lat = proj.unproject(xa + (xb - xa) * t, ya + (yb - ya) * t)
    ele = None
    if a.ele is not None and b.ele is not None:
        ele = a.ele + (b.ele - a.ele) * t
    tm = None
    if a.time is not None and b.time is not None:
        tm = a.time + (b.time - a.time) * t
    return Point(lon=lon, lat=lat, ele=ele, time=tm,
                 seg_index=seg_index, index=-1)


# --------------------------------------------------------------------------- #
# Segment / circle intersection
# --------------------------------------------------------------------------- #
def _segment_circle_intervals(
    xa: float, ya: float, xb: float, yb: float,
    cx: float, cy: float, r: float, eps: float,
) -> list[tuple[float, float]]:
    """Closed t-intervals (t in [0,1]) where segment a->b lies within the
    closed disc of radius r. Detects a chord crossing even when both sampled
    endpoints are outside the circle (quadratic discriminant test)."""
    dx, dy = xb - xa, yb - ya
    fx, fy = xa - cx, ya - cy
    a = dx * dx + dy * dy
    if a <= eps:
        d2 = fx * fx + fy * fy
        return [(0.0, 1.0)] if d2 <= r * r + eps else []
    b = 2.0 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - r * r
    disc = b * b - 4 * a * c
    if disc < -eps * max(1.0, r * r):
        return []
    sq = math.sqrt(max(0.0, disc))
    t1 = (-b - sq) / (2 * a)
    t2 = (-b + sq) / (2 * a)
    # Discriminant ~0: exact tangency at one point, which covers no track
    # length and must not split the segment.
    if abs(t2 - t1) <= 1e-9:
        return []
    lo, hi = min(t1, t2), max(t1, t2)
    lo = max(0.0, lo)
    hi = min(1.0, hi)
    if lo < hi:
        return [(lo, hi)]
    return []


def _merge(intervals: list[tuple[float, float]],
           eps: float = 1e-9) -> list[tuple[float, float]]:
    """Merge overlapping edge-index intervals.

    Coordinates are edge indices (one sample per integer). Touching
    intervals are joined; runs shorter than eps (a tangent contact that
    only clips a sub-nanosecond sliver due to floating-point error) are
    dropped since they cover no meaningful track length.
    """
    if not intervals:
        return []
    iv = sorted(intervals)
    out = [list(iv[0])]
    for lo, hi in iv[1:]:
        if lo <= out[-1][1] + eps:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    result: list[tuple[float, float]] = []
    for lo, hi in out:
        lo = min(1.0e9, max(0.0, lo))
        hi = min(1.0e9, max(0.0, hi))
        if hi - lo > 1e-6:
            result.append((lo, hi))
    return result


@dataclass
class ClipResult:
    visible_segments: list[list[Point]]  # disjoint trksegs for export
    visible_runs: list[list[Point]]     # same data, for map preview
    hidden_runs: list[list[Point]]
    hidden_segments: int
    distance_before_m: float
    distance_after_m: float


def clip_track(
    segments: list[list[Point]],
    proj: LocalProjection,
    coords: list[list[tuple[float, float]]],
    circles: list[CoverCircle],
    eps: float = 1e-7,
) -> ClipResult:
    """Cut every source trkseg where it passes through a cover circle.

    Intersection points split the segment at the circle boundary; their
    timestamps are interpolated proportionally. Original trkseg boundaries
    are never joined, and visible pieces that only touch at a circle
    boundary are kept separate -> mutually disconnected trksegs.
    """
    distance_before = track_distance_m(segments)

    visible: list[list[Point]] = []
    hidden_runs: list[list[Point]] = []
    hidden_count = 0

    for si, seg in enumerate(segments):
        xy = coords[si]
        hidden_intervals: list[tuple[float, float]] = []
        for circle in circles:
            cx, cy = proj.project(circle.lon, circle.lat)
            r = max(0.0, circle.radius)
            for j in range(len(seg) - 1):
                xa, ya = xy[j]
                xb, yb = xy[j + 1]
                for lo, hi in _segment_circle_intervals(
                    xa, ya, xb, yb, cx, cy, r, eps
                ):
                    hidden_intervals.append((j + lo, j + hi))

        merged = _merge(hidden_intervals)
        total_edges = float(len(seg) - 1)

        # Events: 1 = enters cover (end visible), 2 = leaves (start visible).
        events: list[tuple[float, int]] = []
        for lo, hi in merged:
            events.append((lo, 1))
            events.append((hi, 2))
        events.sort(key=lambda e: (e[0], e[1]))

        def point_at(u: float) -> Point:
            """Original sample at integer u, otherwise an interpolation."""
            edge = min(len(seg) - 2, max(0, int(math.floor(u))))
            t = max(0.0, min(1.0, u - edge))
            if t <= 1e-12:
                return seg[edge]
            if t >= 1.0 - 1e-12:
                return seg[edge + 1]
            return interpolate(seg[edge], seg[edge + 1], t, proj, si)

        def build_run(start_u: float, end_u: float,
                      start_pt: Point, end_pt: Point) -> list[Point]:
            """Ordered points covering [start_u, end_u], including every
            original sample whose edge-index lies strictly inside."""
            pts = [start_pt]
            first_k = math.floor(start_u) + 1
            last_k = math.ceil(end_u) - 1
            for k in range(first_k, last_k + 1):
                if start_u + eps < k < end_u - eps:
                    pts.append(seg[k])
            pts.append(end_pt)
            return pts

        inside = False
        start_u = 0.0
        start_pt: Point = seg[0]

        for u, kind in events:
            end_pt = point_at(u)
            if kind == 1:  # entering cover
                if not inside and u - start_u > eps:
                    run = build_run(start_u, u, start_pt, end_pt)
                    visible.append(run)
                start_u, start_pt = u, end_pt
                inside = True
            else:  # leaving cover
                if inside and u - start_u > eps:
                    run = build_run(start_u, u, start_pt, end_pt)
                    hidden_runs.append(run)
                    hidden_count += 1
                start_u, start_pt = u, end_pt
                inside = False

        end_u = total_edges
        end_pt = seg[-1]
        if end_u - start_u > eps:
            run = build_run(start_u, end_u, start_pt, end_pt)
            if inside:
                hidden_runs.append(run)
                hidden_count += 1
            else:
                visible.append(run)

    distance_after = track_distance_m(visible)
    return ClipResult(
        visible_segments=visible,
        visible_runs=visible,
        hidden_runs=hidden_runs,
        hidden_segments=hidden_count,
        distance_before_m=distance_before,
        distance_after_m=distance_after,
    )


# --------------------------------------------------------------------------- #
# GPX export
# --------------------------------------------------------------------------- #
def build_gpx(result: ClipResult, circles: list[CoverCircle]) -> bytes:
    """Serialise visible runs as one track with disjoint trkseg elements."""
    gpx = ET.Element("gpx", {
        "version": "1.1",
        "creator": "hike-trail-share",
        "xmlns": "http://www.topografix.com/GPX/1/1",
    })
    meta = ET.SubElement(gpx, "metadata")
    desc = ET.SubElement(meta, "desc")
    desc.text = (
        f"裁切后轨迹：{result.hidden_segments} 个敏感段已遮蔽，"
        f"共 {len(circles)} 个遮蔽圆。"
    )
    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = "shared-trail-redacted"
    for seg in result.visible_segments:
        trkseg = ET.SubElement(trk, "trkseg")
        for p in seg:
            node = ET.SubElement(trkseg, "trkpt", {
                "lat": f"{p.lat:.7f}", "lon": f"{p.lon:.7f}"
            })
            if p.ele is not None:
                ET.SubElement(node, "ele").text = f"{p.ele:.2f}"
            tm = iso_time(p.time)
            if tm:
                ET.SubElement(node, "time").text = tm

    ET.indent(gpx, space="  ")
    return ET.tostring(gpx, encoding="utf-8", xml_declaration=True)


def new_session_id() -> str:
    return uuid.uuid4().hex
