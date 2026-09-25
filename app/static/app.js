"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const VIEW_W = 1000;
const VIEW_H = 680;
const PAD = 48;
const M_PER_DEG = 111320.0;
const SNAP_PX = 24;

const state = {
  sessionId: null,
  filename: null,
  bounds: null,          // minLon,maxLon,minLat,maxLat,widthM,heightM
  segments: [],          // [[{lon,lat}, ...], ...]
  circles: [],           // {id, lon, lat, radius}
  nextId: 1,
  result: null,
  // projection
  lat0: 0, kx: 1, scale: 1, offX: 0, offY: 0,
  drag: null,
  moved: false,
};

const $ = (id) => document.getElementById(id);
const svg = $("map");

// --------------------------------------------------------------------------- //
// Projection (equirectangular, metres centred on track bounds) -> SVG units
// --------------------------------------------------------------------------- //
function setupProjection() {
  const b = state.bounds;
  state.lat0 = (b.minLat + b.maxLat) / 2;
  const lon0 = (b.minLon + b.maxLon) / 2;
  state.kx = Math.PI / 180 * M_PER_DEG * Math.cos(state.lat0 * Math.PI / 180);
  const ky = Math.PI / 180 * M_PER_DEG;

  const availW = VIEW_W - PAD * 2;
  const availH = VIEW_H - PAD * 2;
  // Inflate zero spans (perfectly horizontal/vertical tracks) so the map
  // still gets a usable scale and some room to place cover circles.
  const realW = b.widthM;
  const realH = b.heightM;
  const maxSpan = Math.max(realW, realH, 1.0);
  const fitW = Math.max(realW, maxSpan * 0.1);
  const fitH = Math.max(realH, maxSpan * 0.1);
  const sx = availW / fitW;
  const sy = availH / fitH;
  state.scale = Math.min(sx, sy);
  // toXY maps relative-to-centre metres, so offsets simply centre the
  // track centre-line in the available viewport.
  state.offX = PAD + availW / 2;
  state.offY = PAD + availH / 2;
}

function lon0() {
  return (state.bounds.minLon + state.bounds.maxLon) / 2;
}

function toXY(lon, lat) {
  const ky = Math.PI / 180 * M_PER_DEG;
  const x = (lon - lon0()) * state.kx * state.scale + state.offX;
  // SVG y grows downward -> invert latitude
  const y = state.offY - (lat - state.lat0) * ky * state.scale;
  return [x, y];
}

function toLonLat(x, y) {
  const ky = Math.PI / 180 * M_PER_DEG;
  const lon = (x - state.offX) / (state.kx * state.scale) + lon0();
  const lat = (state.offY - y) / ky + state.lat0;
  return [lon, lat];
}

function svgPoint(evt) {
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return [0, 0];
  const p = pt.matrixTransform(ctm.inverse());
  return [p.x, p.y];
}

// --------------------------------------------------------------------------- //
// Rendering
// --------------------------------------------------------------------------- //
function el(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function polylineFor(points, className) {
  const d = points.map((p, i) => {
    const [x, y] = toXY(p.lon, p.lat);
    return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join("");
  return el("path", { d, class: className });
}

function renderTracks() {
  clear($("layer-original"));
  clear($("layer-visible"));
  clear($("layer-hidden"));

  for (const seg of state.segments) {
    $("layer-original").appendChild(polylineFor(seg, "track-original"));
  }

  if (state.result) {
    for (const run of state.result.hiddenRuns) {
      $("layer-hidden").appendChild(polylineFor(run.coordinates.map(
        ([lon, lat]) => ({ lon, lat })), "track-hidden"));
    }
    for (const run of state.result.visibleRuns) {
      $("layer-visible").appendChild(polylineFor(run.coordinates.map(
        ([lon, lat]) => ({ lon, lat })), "track-visible"));
    }
  }
}

function renderCircles() {
  const g = $("layer-circles");
  clear(g);
  state.circles.forEach((c, idx) => {
    const [x, y] = toXY(c.lon, c.lat);
    const rPx = Math.max(2, c.radius * state.scale);
    const grp = el("g", { "data-id": String(c.id) });
    grp.appendChild(el("circle", {
      cx: x, cy: y, r: rPx, class: "cover-circle",
    }));
    grp.appendChild(el("circle", {
      cx: x, cy: y, r: 5, class: "cover-dot",
    }));
    const label = el("text", { x, y: y - rPx - 6, class: "cover-label" });
    label.textContent = `#${idx + 1}  ${Math.round(c.radius)}m`;
    grp.appendChild(label);
    g.appendChild(grp);
  });
}

function niceDistance(value) {
  // choose a 1/2/5 x power-of-ten round number near `value`
  const pow = Math.pow(10, Math.floor(Math.log10(value)));
  const n = value / pow;
  const f = n >= 5 ? 5 : n >= 2 ? 2 : 1;
  return f * pow;
}

function renderScaleBar() {
  const g = $("scalebar");
  clear(g);
  // 120 svg units correspond to:
  const targetM = niceDistance(120 / state.scale);
  const px = targetM * state.scale;
  const x0 = VIEW_W - PAD - px;
  const y = VIEW_H - 24;
  g.appendChild(el("line", {
    x1: x0, y1: y, x2: x0 + px, y2: y,
    stroke: "#334155", "stroke-width": 2,
  }));
  g.appendChild(el("line", { x1: x0, y1: y - 5, x2: x0, y2: y + 5, stroke: "#334155", "stroke-width": 2 }));
  g.appendChild(el("line", { x1: x0 + px, y1: y - 5, x2: x0 + px, y2: y + 5, stroke: "#334155", "stroke-width": 2 }));
  const t = el("text", { x: x0 + px / 2, y: y - 8, "text-anchor": "middle", "font-size": 12, fill: "#334155" });
  t.textContent = targetM >= 1000 ? `${targetM / 1000} km` : `${Math.round(targetM)} m`;
  g.appendChild(t);
}

function renderCircleList() {
  const ul = $("circle-list");
  clear(ul);
  state.circles.forEach((c, idx) => {
    const li = document.createElement("li");

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `#${idx + 1}  ${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`;

    const radius = document.createElement("input");
    radius.type = "number";
    radius.min = "5"; radius.max = "100000"; radius.step = "5";
    radius.value = String(c.radius);
    radius.title = "遮蔽半径（米）";
    radius.addEventListener("input", () => {
      const v = parseFloat(radius.value);
      if (Number.isFinite(v) && v > 0) {
        c.radius = v;
        renderCircles();
      }
    });

    const del = document.createElement("button");
    del.className = "del";
    del.title = "删除";
    del.textContent = "✕";
    del.addEventListener("click", () => {
      state.circles = state.circles.filter((x) => x.id !== c.id);
      renderAll();
    });

    li.append(meta, radius, del);
    ul.appendChild(li);
  });
}

function renderAll() {
  renderTracks();
  renderCircles();
  renderCircleList();
  renderScaleBar();
}

// --------------------------------------------------------------------------- //
// Map interactions: click to add, drag to move circles
// --------------------------------------------------------------------------- //
function nearestTrackPointPx(x, y) {
  let best = null;
  let bestD = SNAP_PX;
  for (const seg of state.segments) {
    for (let i = 0; i < seg.length - 1; i++) {
      const [ax, ay] = toXY(seg[i].lon, seg[i].lat);
      const [bx, by] = toXY(seg[i + 1].lon, seg[i + 1].lat);
      const dx = bx - ax, dy = by - ay;
      const len2 = dx * dx + dy * dy || 1e-9;
      let t = ((x - ax) * dx + (y - ay) * dy) / len2;
      t = Math.max(0, Math.min(1, t));
      const px = ax + t * dx, py = ay + t * dy;
      const d = Math.hypot(px - x, py - y);
      if (d < bestD) {
        bestD = d;
        best = {
          lon: seg[i].lon + t * (seg[i + 1].lon - seg[i].lon),
          lat: seg[i].lat + t * (seg[i + 1].lat - seg[i].lat),
        };
      }
    }
  }
  return best;
}

function circleIdFromTarget(node) {
  let n = node;
  while (n && n !== svg) {
    if (n.dataset && n.dataset.id !== undefined) return Number(n.dataset.id);
    n = n.parentNode;
  }
  return null;
}

svg.addEventListener("mousedown", (evt) => {
  if (!state.sessionId) return;
  const id = circleIdFromTarget(evt.target);
  if (id !== null) {
    state.drag = { id };
    state.moved = false;
    evt.preventDefault();
  }
});

window.addEventListener("mousemove", (evt) => {
  const [x, y] = svgPoint(evt);
  if (state.sessionId) {
    const [lon, lat] = toLonLat(x, y);
    $("coord-readout").textContent = `经度 ${lon.toFixed(6)}　纬度 ${lat.toFixed(6)}`;
  }
  if (state.drag) {
    const c = state.circles.find((z) => z.id === state.drag.id);
    if (c) {
      const [lon, lat] = toLonLat(x, y);
      c.lon = lon; c.lat = lat;
      state.moved = true;
      renderCircles();
    }
  }
});

window.addEventListener("mouseup", () => {
  if (state.drag) {
    if (state.moved) renderCircleList();
    state.drag = null;
  }
});

// Click on empty map area: add a cover circle (ignored after a drag).
svg.addEventListener("click", (evt) => {
  if (!state.sessionId || state.drag || circleIdFromTarget(evt.target) !== null) return;
  const [x, y] = svgPoint(evt);
  let lon, lat;
  const snap = nearestTrackPointPx(x, y);
  if (snap) ({ lon, lat } = snap);
  else [lon, lat] = toLonLat(x, y);

  state.circles.push({
    id: state.nextId++,
    lon, lat,
    radius: parseFloat($("default-radius").value) || 50,
  });
  renderAll();
});

$("clear-btn").addEventListener("click", () => {
  state.circles = [];
  renderAll();
});

// --------------------------------------------------------------------------- //
// API
// --------------------------------------------------------------------------- //
function setStatus(text, isError = false) {
  const node = $("map-status");
  node.textContent = text;
  node.classList.toggle("toast-error", isError);
}

$("upload-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const file = $("file-input").files[0];
  if (!file) {
    $("upload-info").textContent = "请先选择 .gpx 文件。";
    $("upload-info").className = "hint toast-error";
    return;
  }
  $("upload-btn").disabled = true;
  $("upload-info").textContent = "上传解析中…";
  $("upload-info").className = "hint";

  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    if (!res.ok) throw new Error((await res.json()).detail || "上传失败");
    const data = await res.json();
    state.sessionId = data.sessionId;
    state.filename = data.filename;
    state.segments = data.segments.map((s) => s.points);
    state.bounds = data.bounds;
    state.circles = [];
    state.result = null;
    setupProjection();
    renderAll();
    $("circle-card").hidden = false;
    $("result-card").hidden = true;
    const nPts = state.segments.reduce((n, s) => n + s.length, 0);
    $("upload-info").textContent =
      `已加载 ${file.name}：${state.segments.length} 个原始分段、${nPts} 个轨迹点。`;
    setStatus("在预览图上点击以添加遮蔽点，然后点击“应用裁切”。");
  } catch (err) {
    $("upload-info").textContent = err.message;
    $("upload-info").className = "hint toast-error";
  } finally {
    $("upload-btn").disabled = false;
  }
});

function fmtDistance(m) {
  if (m >= 1000) return `${(m / 1000).toFixed(2)} km`;
  return `${Math.round(m)} m`;
}

$("clip-btn").addEventListener("click", async () => {
  if (!state.sessionId) return;
  if (state.circles.length === 0) {
    setStatus("请先添加至少一个遮蔽点。", true);
    return;
  }
  const btn = $("clip-btn");
  btn.disabled = true;
  setStatus("正在计算轨迹线段与遮蔽圆的交点并裁切…");
  try {
    const res = await fetch("/api/clip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        circles: state.circles.map((c) => ({
          lon: c.lon, lat: c.lat, radius: c.radius,
        })),
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "裁切失败");
    state.result = await res.json();
    renderTracks();
    $("stat-hidden").textContent = String(state.result.hiddenSegments);
    $("stat-before").textContent = fmtDistance(state.result.distanceBeforeM);
    $("stat-after").textContent = fmtDistance(state.result.distanceAfterM);
    const removed = state.result.distanceRemovedM;
    const pct = state.result.distanceBeforeM > 0
      ? (removed / state.result.distanceBeforeM * 100).toFixed(1) : "0.0";
    $("stat-removed").textContent = `${fmtDistance(removed)}（${pct}%）`;
    $("stat-segs").textContent = String(state.result.visibleTrksegCount);
    $("result-card").hidden = false;
    setStatus(`裁切完成：隐藏 ${state.result.hiddenSegments} 段，` +
      `导出 ${state.result.visibleTrksegCount} 个互不连接的 trkseg。`);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

$("download-btn").addEventListener("click", () => {
  if (!state.sessionId) return;
  const a = document.createElement("a");
  a.href = `/api/download/${state.sessionId}`;
  a.download = "trail-redacted.gpx";
  document.body.appendChild(a);
  a.click();
  a.remove();
});
