"""Standalone verification of GPX parsing/clipping (stdlib only)."""
import sys, math
sys.path.insert(0, "/workspace")
from app.geo import (
    parse_gpx, LocalProjection, CoverCircle, clip_track, build_gpx,
    track_distance_m, iso_time,
)
import xml.etree.ElementTree as ET

# Build a GPX: straight east-west track along lat 30.0, from lon 120.000 to 120.010
# points every ~0.001 deg lon. Times at 1-minute intervals, ele ramp.
gpx = ['<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">',
       '<trk><name>t</name>']
pts = []
for i in range(11):
    lon = 120.000 + i * 0.001
    tm = f"2026-01-01T00:{i:02d}:00Z"
    pts.append((lon, 30.0, float(i * 10), tm))
gpx.append('<trkseg>')
for lon, lat, ele, tm in pts:
    gpx.append(f'<trkpt lat="{lat}" lon="{lon}"><ele>{ele}</ele><time>{tm}</time></trkpt>')
gpx.append('</trkseg></trk></gpx>')
data = "".join(gpx).encode()

segments = parse_gpx(data)
assert len(segments) == 1 and len(segments[0]) == 11
allpts = [p for s in segments for p in s]
proj = LocalProjection(allpts)
coords = [[proj.project(p.lon, p.lat) for p in seg] for seg in segments]

d_before = track_distance_m(segments)
print("distance before:", round(d_before, 1), "m")

# Circle mid-edge: center between samples i=4(lon120.004) and i=5(lon120.005),
# radius 30m. Both samples are ~96m apart*... 0.001deg lon at lat30 ~= 96.25m.
# Half chord... center at 120.0045, r=30 -> interval well within edge interior,
# both sampled endpoints OUTSIDE the circle. This tests crossing detection.
cx_lon, cy_lat = 120.0045, 30.0000
# small lat offset to also force crossing: keep on-line so chord = diameter 60m
circles = [CoverCircle(cx_lon, cy_lat, 30.0)]
res = clip_track(segments, proj, coords, circles)
print("CASE 1 mid-edge crossing (both endpoints outside):")
print("  hidden segments:", res.hidden_segments)
print("  visible trksegs:", len(res.visible_segments))
print("  visible pt counts:", [len(s) for s in res.visible_segments])
print("  hidden run pts:", len(res.hidden_runs[0]))
assert res.hidden_segments == 1
assert len(res.visible_segments) == 2
# visible run 1: samples 0..4 + exit? No: enter happens on edge 4-5.
# first visible = orig points 0..4 + circle-entry point -> 6 pts
assert len(res.visible_segments[0]) == 6
assert len(res.visible_segments[1]) == 7  # exit point + samples 5..10
hidden = res.hidden_runs[0]
assert len(hidden) == 2  # entry and exit interpolated points
h0, h1 = hidden
cx, cy = proj.project(cx_lon, cy_lat)
for hp in hidden:
    hx, hy = proj.project(hp.lon, hp.lat)
    dist = math.hypot(hx - cx, hy - cy)
    assert abs(dist - 30.0) < 1e-4, ("not on circle", dist)
# time interpolation: entry t along edge 4->5, measured in projected metres
(xa, ya), (xb, yb) = coords[0][4], coords[0][5]
edge_len = math.hypot(xb - xa, yb - ya)
t_enter = (edge_len / 2 - 30.0) / edge_len
exp_sec = (4 * 60) + t_enter * 60
got = (h0.time - h0.time.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() if False else None
# compare against 00:00 base
from datetime import datetime, timezone
base = datetime(2026,1,1,tzinfo=timezone.utc)
got = (h0.time - base).total_seconds()
print("  entry time offset sec:", round(got,2), "expected ~", round(exp_sec,2))
assert abs(got - exp_sec) < 0.5
# ele interpolation check
assert abs(h0.ele - (40 + t_enter * 10)) < 1e-6
# hidden length ~ diameter 60m
from app.geo import track_distance_m as td
assert abs(td([hidden]) - 60.0) < 1e-3
print("  hidden length:", round(td([hidden]),3), "m (expect 60)")
assert abs(res.distance_before_m - d_before) < 1e-9
assert abs(res.distance_after_m - (d_before - 60.0)) < 1e-3
# disjoint: last pt of run0 and first pt of run1 must differ (boundary points distinct)
assert not (res.visible_segments[0][-1].lon == res.visible_segments[1][0].lon and
            res.visible_segments[0][-1].lat == res.visible_segments[1][0].lat)

# CASE 2: circle covering a sample point junction (center on sample i=5, r=40m)
circles2 = [CoverCircle(120.005, 30.0, 40.0)]
res2 = clip_track(segments, proj, coords, circles2)
print("CASE 2 junction coverage: hidden =", res2.hidden_segments,
      "visible segs =", len(res2.visible_segments))
assert res2.hidden_segments == 1
assert len(res2.visible_segments) == 2
# entry on edge 4->5, exit on edge 5->6; hidden run includes interior sample 5
hr = res2.hidden_runs[0]
assert len(hr) == 3
assert abs(td([hr]) - 80.0) < 1e-3
print("  hidden length:", round(td([hr]), 3), "(expect 80)")

# CASE 3: two overlapping circles merge into one hidden run
# centres one sample (~96m) apart; radius 55m makes their chords overlap
circles3 = [CoverCircle(120.003, 30.0, 55.0), CoverCircle(120.004, 30.0, 55.0)]
res3 = clip_track(segments, proj, coords, circles3)
print("CASE 3 overlapping circles: hidden runs =", res3.hidden_segments)
assert res3.hidden_segments == 1
assert len(res3.visible_segments) == 2

# CASE 4: circle fully beyond track -> no change
res4 = clip_track(segments, proj, coords, [CoverCircle(121.0, 31.0, 100.0)])
assert res4.hidden_segments == 0
assert len(res4.visible_segments) == 1
assert abs(res4.distance_after_m - d_before) < 1e-9

# CASE 5: circle covers entire segment endpoints (from before first to after last)
res5 = clip_track(segments, proj, coords, [CoverCircle(120.005, 30.0, 2000.0)])
assert res5.hidden_segments == 1
assert len(res5.visible_segments) == 0
assert res5.distance_after_m == 0.0

# CASE 6: two disjoint circles -> 2 hidden, 3 visible
res6 = clip_track(segments, proj, coords,
                  [CoverCircle(120.002, 30.0, 20.0), CoverCircle(120.008, 30.0, 20.0)])
print("CASE 6 disjoint: hidden =", res6.hidden_segments, "visible =", len(res6.visible_segments))
assert res6.hidden_segments == 2
assert len(res6.visible_segments) == 3

# CASE 7: tangent touch (circle tangent at a sample) -> zero-length interval skipped
nx, ny = proj.project(120.005, 30.0)
tlon, tlat = proj.unproject(nx, ny + 10.0)  # exactly 10m north
res7 = clip_track(segments, proj, coords, [CoverCircle(tlon, tlat, 10.0)])
print("CASE 7 tangent: hidden =", res7.hidden_segments)
# tangent point exactly on sample -> zero-length interval, no hidden run
assert res7.hidden_segments == 0

# GPX export parses back, has disjoint trksegs, monotonic times
out = build_gpx(res6, circles3)
root = ET.fromstring(out)
ns = {"g": "http://www.topografix.com/GPX/1/1"}
trksegs = root.findall(".//g:trkseg", ns)
print("exported trkseg count:", len(trksegs))
assert len(trksegs) == 3
for trkseg in trksegs:
    pnodes = trkseg.findall("g:trkpt", ns)
    times = [p.find("g:time", ns).text for p in pnodes]
    assert times == sorted(times), "times not sorted"
    assert pnodes[0].find("g:ele", ns) is not None
# boundary endpoints present: first seg ends on circle, second seg starts on another circle
first = trksegs[0].findall("g:trkpt", ns)[-1]
second = trksegs[1].findall("g:trkpt", ns)[0]
assert first.attrib["lon"] != second.attrib["lon"] or first.attrib["lat"] != second.attrib["lat"]

# CASE 8: no-time GPX still clips and exports without time
gpx_nt = ['<?xml version="1.0"?><gpx version="1.1"><trk><trkseg>']
for i in range(5):
    gpx_nt.append(f'<trkpt lat="30.0" lon="{120.0+i*0.001}"></trkpt>')
gpx_nt.append('</trkseg></trk></gpx>')
segs_nt = parse_gpx("".join(gpx_nt).encode())
p2 = [p for s in segs_nt for p in s]
pr2 = LocalProjection(p2)
co2 = [[pr2.project(p.lon,p.lat) for p in s] for s in segs_nt]
r8 = clip_track(segs_nt, pr2, co2, [CoverCircle(120.002, 30.0, 30.0)])
assert r8.hidden_segments == 1 and len(r8.visible_segments) == 2
out8 = build_gpx(r8, [])
root8 = ET.fromstring(out8)
assert root8.findall(".//g:time", {"g": "http://www.topografix.com/GPX/1/1"}) == []
print("CASE 8 no-time GPX clips and exports without <time> OK")

# CASE 9: two original trkseg boundaries must never join even when both visible
gpx_two = ['<?xml version="1.0"?><gpx version="1.1"><trk>']
for which in range(2):
    gpx_two.append('<trkseg>')
    for i in range(3):
        lon = 120.0 + which*0.01 + i*0.001
        gpx_two.append(f'<trkpt lat="30.0" lon="{lon}"><time>2026-01-01T0{which}:{i:02d}:00Z</time></trkpt>')
    gpx_two.append('</trkseg>')
gpx_two.append('</trk></gpx>')
segs9 = parse_gpx("".join(gpx_two).encode())
assert len(segs9) == 2
p9 = [p for s in segs9 for p in s]
pr9 = LocalProjection(p9)
co9 = [[pr9.project(p.lon,p.lat) for p in s] for s in segs9]
r9 = clip_track(segs9, pr9, co9, [])  # no circles
assert len(r9.visible_segments) == 2
r9b = clip_track(segs9, pr9, co9, [CoverCircle(120.0015, 30.0, 20.0)])  # splits first seg only
assert len(r9b.visible_segments) == 3
print("CASE 9 original boundaries preserved (2 segs; split first ->", len(r9b.visible_segments), ")")

print("\nALL GEOMETRY TESTS PASSED")
