"""FastAPI backend: upload GPX, preview cover-circle clipping, download redacted GPX."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .geo import (
    ClipResult,
    CoverCircle,
    LocalProjection,
    Point,
    build_gpx,
    clip_track,
    new_session_id,
    parse_gpx,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SESSION_TTL_SECONDS = float(os.environ.get("SESSION_TTL_SECONDS", str(3 * 3600)))
MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "20"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------- #
# In-memory session store (sufficient for a single-container deployment)
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    segments: list[list[Point]]
    proj: LocalProjection
    coords: list[list[tuple[float, float]]]
    created_at: float
    last_result: ClipResult | None = None
    last_circles: list[CoverCircle] | None = None


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _purge_expired(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    expired = [sid for sid, s in _sessions.items()
               if now - s.created_at > SESSION_TTL_SECONDS]
    for sid in expired:
        _sessions.pop(sid, None)


def get_session(session_id: str) -> Session:
    with _sessions_lock:
        _purge_expired()
        session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传 GPX。")
    return session


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class CircleIn(BaseModel):
    lon: float
    lat: float
    radius: float = Field(gt=0, le=100_000)


class ClipIn(BaseModel):
    session_id: str
    circles: list[CircleIn]


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #
def _run_geojson(run: list[Point]) -> dict[str, Any]:
    return {
        "type": "LineString",
        "coordinates": [[p.lon, p.lat] for p in run],
    }


def _segments_preview(session: Session) -> dict[str, Any]:
    s = session
    xmin, ymin, xmax, ymax = s.proj.bounds_m
    return {
        "segments": [
            {"points": [p.to_dict() for p in seg]} for seg in s.segments
        ],
        "bounds": {
            "minLon": s.proj.min_lon,
            "maxLon": s.proj.max_lon,
            "minLat": s.proj.min_lat,
            "maxLat": s.proj.max_lat,
            "widthM": xmax - xmin,
            "heightM": ymax - ymin,
        },
    }


def _clip_payload(result: ClipResult, circles: list[CoverCircle]) -> dict[str, Any]:
    return {
        "hiddenSegments": result.hidden_segments,
        "distanceBeforeM": round(result.distance_before_m, 1),
        "distanceAfterM": round(result.distance_after_m, 1),
        "distanceRemovedM": round(
            result.distance_before_m - result.distance_after_m, 1
        ),
        "visibleRuns": [_run_geojson(r) for r in result.visible_runs],
        "hiddenRuns": [_run_geojson(r) for r in result.hidden_runs],
        "visibleTrksegCount": len(result.visible_segments),
    }


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
app = FastAPI(title="徒步轨迹遮蔽分享")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    filename = file.filename or "track.gpx"
    if not filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="请上传 .gpx 文件。")

    raw = await file.read()
    limit = int(MAX_UPLOAD_MB * 1024 * 1024)
    if len(raw) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {MAX_UPLOAD_MB:g} MB）。",
        )
    try:
        segments = parse_gpx(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    all_points = [p for seg in segments for p in seg]
    proj = LocalProjection(all_points)
    coords = [[proj.project(p.lon, p.lat) for p in seg] for seg in segments]

    sid = new_session_id()
    with _sessions_lock:
        _purge_expired()
        _sessions[sid] = Session(
            segments=segments,
            proj=proj,
            coords=coords,
            created_at=time.time(),
        )

    payload = {"sessionId": sid, "filename": filename}
    payload.update(_segments_preview(_sessions[sid]))
    return JSONResponse(payload)


@app.post("/api/clip")
def clip(body: ClipIn) -> JSONResponse:
    session = get_session(body.session_id)
    circles = [CoverCircle(lon=c.lon, lat=c.lat, radius=float(c.radius))
               for c in body.circles]
    for c in circles:
        if not (-180.0 <= c.lon <= 180.0 and -90.0 <= c.lat <= 90.0):
            raise HTTPException(status_code=400, detail="遮蔽圆心坐标超出范围。")

    result = clip_track(session.segments, session.proj, session.coords, circles)
    session.last_result = result
    session.last_circles = circles
    return JSONResponse(_clip_payload(result, circles))


@app.get("/api/download/{session_id}")
def download(session_id: str) -> Response:
    session = get_session(session_id)
    if session.last_result is None or session.last_circles is None:
        raise HTTPException(status_code=409, detail="请先执行裁切再下载。")
    gpx_bytes = build_gpx(session.last_result, session.last_circles)
    return Response(
        content=gpx_bytes,
        media_type="application/gpx+xml",
        headers={
            "Content-Disposition": 'attachment; filename="trail-redacted.gpx"'
        },
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
