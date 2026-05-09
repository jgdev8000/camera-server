#!/usr/bin/env python3
"""Multi-camera FastAPI server for EPICS Basler cameras (USB and GigE).

Reads images from EPICS PVs, serves MJPEG streams and snapshots, and exposes
exposure / gain controls via caput. Cameras are configured in cameras.json
and can be added or removed at runtime.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

try:
    from epics import PV, caget, caput
    EPICS_AVAILABLE = True
except ImportError:
    EPICS_AVAILABLE = False
    PV = caget = caput = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("CAMERAS_CONFIG", Path(__file__).parent / "cameras.json"))

# Bind to the host's interface on the camera subnet by default. Override with HOST env var.
BIND_SUBNET = os.environ.get("CAMERA_SERVER_SUBNET", "192.168.1.0/24")


def resolve_bind_host() -> str:
    explicit = os.environ.get("HOST")
    if explicit:
        return explicit
    network = ipaddress.ip_network(BIND_SUBNET, strict=False)
    # ask the kernel which local IP would be used to reach an address in the subnet
    probe = str(next(network.hosts()))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe, 1))
        candidate = s.getsockname()[0]
    finally:
        s.close()
    if ipaddress.ip_address(candidate) in network:
        return candidate
    logger.warning("No local interface in %s; falling back to 0.0.0.0", BIND_SUBNET)
    return "0.0.0.0"


# ---------- models ----------

class CameraConfig(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    label: str
    type: Literal["usb", "gige"]
    prefix: str
    fps: int = Field(default=12, ge=1, le=60)
    width: int = Field(default=640, ge=16)
    height: int = Field(default=480, ge=16)

    @field_validator("prefix")
    @classmethod
    def _prefix_ends_with_colon(cls, v: str) -> str:
        if not v.endswith(":"):
            raise ValueError("prefix must end with ':'")
        return v

    def pv(self, suffix: str) -> str:
        return f"{self.prefix}{suffix}"


AutoMode = Literal["Off", "Once", "Continuous"]


class ControlUpdate(BaseModel):
    exposure: Optional[float] = None
    gain: Optional[float] = None
    acquire: Optional[bool] = None
    exposure_auto: Optional[AutoMode] = None
    gain_auto: Optional[AutoMode] = None


# ---------- camera stream ----------

class CameraStream:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.running = False
        self.current_frame: Optional[np.ndarray] = None
        self.frame_counter = 0  # incremented on every new frame; lets MJPEG generators wait without polling
        self.lock = threading.Lock()
        self._last_live_frame_at = 0.0
        self._pv_image: Optional["PV"] = None
        self._pv_y: Optional["PV"] = None
        self._pv_x: Optional["PV"] = None
        self._demo_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        if EPICS_AVAILABLE:
            self._pv_image = PV(self.cfg.pv("image1:ArrayData"),
                                auto_monitor=True, callback=self._on_frame)
            self._pv_y = PV(self.cfg.pv("cam1:ArraySizeY_RBV"), auto_monitor=True)
            self._pv_x = PV(self.cfg.pv("cam1:ArraySizeX_RBV"), auto_monitor=True)
        self._demo_thread = threading.Thread(
            target=self._demo_fallback_loop, daemon=True, name=f"camera-{self.cfg.id}-demo"
        )
        self._demo_thread.start()
        logger.info("Camera %s started (prefix=%s, monitor=%s)",
                    self.cfg.id, self.cfg.prefix, EPICS_AVAILABLE)

    def stop(self) -> None:
        self.running = False
        for pv in (self._pv_image, self._pv_y, self._pv_x):
            if pv is not None:
                try:
                    pv.disconnect()
                except Exception:
                    pass
        self._pv_image = self._pv_y = self._pv_x = None
        logger.info("Camera %s stopped", self.cfg.id)

    def _on_frame(self, value=None, **_kw) -> None:
        # called by pyepics monitor thread on every new image post — keep it lean
        if value is None or getattr(value, "size", 0) == 0:
            return
        try:
            h = self._pv_y.value if self._pv_y is not None else None
            w = self._pv_x.value if self._pv_x is not None else None
            if not (h and w and int(h) > 0 and int(w) > 0):
                return  # IOC up but camera not acquiring yet
            ih, iw = int(h), int(w)
            data = value
            if data.ndim == 1:
                if data.size < ih * iw:
                    return
                frame = data[: ih * iw].reshape((ih, iw))
            else:
                frame = data
            if frame.dtype != np.uint8:
                frame = np.uint8(frame)
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            frame = cv2.resize(frame, (self.cfg.width, self.cfg.height))
            with self.lock:
                self.current_frame = frame
                self._last_live_frame_at = time.time()
                self.frame_counter += 1
        except Exception as e:
            logger.debug("Camera %s on_frame error: %s", self.cfg.id, e)

    def _demo_fallback_loop(self) -> None:
        # produces demo frames only when no live frame has arrived recently
        period = max(0.1, 1.0 / self.cfg.fps)
        while self.running:
            time.sleep(period)
            if time.time() - self._last_live_frame_at < 1.0:
                continue
            with self.lock:
                self.current_frame = self._demo_frame()
                self.frame_counter += 1

    def _demo_frame(self) -> np.ndarray:
        t = int(time.time() * 100) % 256
        gradient = ((np.arange(self.cfg.height)[:, np.newaxis]
                     + np.arange(self.cfg.width)[np.newaxis, :]
                     + t) % 256).astype(np.uint8)
        frame = np.stack([gradient, gradient, gradient], axis=2)
        cv2.putText(frame, f"{self.cfg.id} (demo)", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(frame, f"prefix={self.cfg.prefix}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        return frame

    def get_frame(self) -> Optional[np.ndarray]:
        with self.lock:
            return self.current_frame.copy() if self.current_frame is not None else None

    def get_frame_if_newer(self, last_seen: int):
        """Return (frame, frame_counter) if a new frame is available, else (None, last_seen)."""
        with self.lock:
            if self.frame_counter == last_seen:
                return None, last_seen
            frame = self.current_frame.copy() if self.current_frame is not None else None
            return frame, self.frame_counter

    def is_connected(self) -> bool:
        return time.time() - self._last_live_frame_at < 2.0

    # control

    def read_control(self) -> dict:
        if not EPICS_AVAILABLE:
            return {"exposure": None, "gain": None, "acquire": None,
                    "exposure_auto": None, "gain_auto": None}
        return {
            "exposure": _safe_caget(self.cfg.pv("cam1:AcquireTime_RBV")),
            "gain": _safe_caget(self.cfg.pv("cam1:Gain_RBV")),
            "acquire": _safe_caget_bool(self.cfg.pv("cam1:Acquire")),
            "exposure_auto": _safe_caget_str(self.cfg.pv("cam1:GC_ExposureAuto")),
            "gain_auto": _safe_caget_str(self.cfg.pv("cam1:GC_GainAuto")),
        }

    def set_control(self, update: ControlUpdate) -> dict:
        if not EPICS_AVAILABLE:
            raise HTTPException(status_code=503, detail="pyepics not available")
        applied = {}
        # set auto modes first so a paired manual value below isn't overridden
        if update.exposure_auto is not None:
            ok = caput(self.cfg.pv("cam1:GC_ExposureAuto"), update.exposure_auto)
            if ok != 1:
                raise HTTPException(status_code=502, detail="caput ExposureAuto failed")
            applied["exposure_auto"] = update.exposure_auto
        if update.gain_auto is not None:
            ok = caput(self.cfg.pv("cam1:GC_GainAuto"), update.gain_auto)
            if ok != 1:
                raise HTTPException(status_code=502, detail="caput GainAuto failed")
            applied["gain_auto"] = update.gain_auto
        if update.exposure is not None:
            ok = caput(self.cfg.pv("cam1:AcquireTime"), float(update.exposure))
            if ok != 1:
                raise HTTPException(status_code=502, detail="caput AcquireTime failed")
            applied["exposure"] = update.exposure
        if update.gain is not None:
            ok = caput(self.cfg.pv("cam1:Gain"), float(update.gain))
            if ok != 1:
                raise HTTPException(status_code=502, detail="caput Gain failed")
            applied["gain"] = update.gain
        if update.acquire is not None:
            ok = caput(self.cfg.pv("cam1:Acquire"), 1 if update.acquire else 0)
            if ok != 1:
                raise HTTPException(status_code=502, detail="caput Acquire failed")
            applied["acquire"] = update.acquire
        return applied


def _safe_caget(pv: str):
    try:
        v = caget(pv, timeout=1.0)
        return float(v) if v is not None else None
    except Exception:
        return None


def _safe_caget_bool(pv: str):
    try:
        v = caget(pv, timeout=1.0, as_string=False)
        return bool(v) if v is not None else None
    except Exception:
        return None


def _safe_caget_str(pv: str):
    try:
        v = caget(pv, timeout=1.0, as_string=True)
        return str(v) if v is not None else None
    except Exception:
        return None


# ---------- manager ----------

class CameraManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.streams: dict[str, CameraStream] = {}
        self.default: Optional[str] = None
        self._lock = threading.Lock()

    def load(self) -> None:
        if not self.config_path.exists():
            logger.warning("No %s found; starting with no cameras", self.config_path)
            return
        data = json.loads(self.config_path.read_text())
        self.default = data.get("default")
        for entry in data.get("cameras", []):
            cfg = CameraConfig(**entry)
            stream = CameraStream(cfg)
            stream.start()
            self.streams[cfg.id] = stream
        if self.default and self.default not in self.streams:
            logger.warning("default camera %s not in cameras list", self.default)
            self.default = None
        logger.info("Loaded %d cameras (default=%s)", len(self.streams), self.default)

    def save(self) -> None:
        with self._lock:
            payload = {
                "default": self.default,
                "cameras": [s.cfg.model_dump() for s in self.streams.values()],
            }
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp, self.config_path)

    def add(self, cfg: CameraConfig) -> CameraStream:
        if cfg.id in self.streams:
            raise HTTPException(status_code=409, detail=f"camera id '{cfg.id}' already exists")
        stream = CameraStream(cfg)
        stream.start()
        self.streams[cfg.id] = stream
        if self.default is None:
            self.default = cfg.id
        self.save()
        return stream

    def remove(self, camera_id: str) -> None:
        stream = self.streams.pop(camera_id, None)
        if stream is None:
            raise HTTPException(status_code=404, detail=f"camera '{camera_id}' not found")
        stream.stop()
        if self.default == camera_id:
            self.default = next(iter(self.streams), None)
        self.save()

    def get(self, camera_id: str) -> CameraStream:
        stream = self.streams.get(camera_id)
        if stream is None:
            raise HTTPException(status_code=404, detail=f"camera '{camera_id}' not found")
        return stream

    def get_default(self) -> CameraStream:
        if self.default is None:
            raise HTTPException(status_code=503, detail="no default camera configured")
        return self.get(self.default)

    def list_status(self) -> list[dict]:
        return [
            {
                **s.cfg.model_dump(),
                "status": "connected" if s.is_connected() else "initializing",
                "is_default": cid == self.default,
            }
            for cid, s in self.streams.items()
        ]

    def shutdown(self) -> None:
        for s in self.streams.values():
            s.stop()


# ---------- app ----------

manager = CameraManager(CONFIG_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.load()
    yield
    manager.shutdown()


app = FastAPI(title="EPICS Multi-Camera Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _mjpeg_response(stream: CameraStream) -> StreamingResponse:
    async def gen():
        last_seen = -1
        while stream.running:
            frame, last_seen = stream.get_frame_if_newer(last_seen)
            if frame is None:
                await asyncio.sleep(0.005)
                continue
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(buf)).encode() + b"\r\n\r\n"
                       + buf.tobytes() + b"\r\n")

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


def _snapshot_response(stream: CameraStream, ext: str) -> Response:
    frame = stream.get_frame()
    if frame is None:
        raise HTTPException(status_code=503, detail="camera not ready")
    ok, buf = cv2.imencode(f".{ext}", frame,
                           [cv2.IMWRITE_JPEG_QUALITY, 95] if ext == "jpg" else [])
    if not ok:
        raise HTTPException(status_code=500, detail="encode failed")
    media = "image/jpeg" if ext == "jpg" else "image/png"
    return Response(content=buf.tobytes(), media_type=media)


def _info_payload(stream: CameraStream) -> dict:
    cfg = stream.cfg
    return {
        "id": cfg.id,
        "label": cfg.label,
        "type": cfg.type,
        "prefix": cfg.prefix,
        "resolution": {"width": cfg.width, "height": cfg.height},
        "fps": cfg.fps,
        "epics_pv": {
            "image": cfg.pv("image1:ArrayData"),
            "height": cfg.pv("cam1:ArraySizeY_RBV"),
            "width": cfg.pv("cam1:ArraySizeX_RBV"),
            "exposure": cfg.pv("cam1:AcquireTime"),
            "gain": cfg.pv("cam1:Gain"),
            "acquire": cfg.pv("cam1:Acquire"),
        },
    }


# ---------- landing page ----------

VIEW_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__CAM_ID__ — live view</title>
<style>
  html, body { height: 100%; margin: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0b0f14; color: #f3f4f6; display: flex; flex-direction: column; }
  .topbar { display: flex; justify-content: space-between; align-items: center;
            padding: 10px 16px; background: #111827; border-bottom: 1px solid #1f2937; }
  .topbar a { color: #93c5fd; text-decoration: none; font-size: 13px; }
  .title { font-weight: 600; font-size: 15px; }
  .title-sub { color: #9ca3af; font-size: 12px; margin-left: 8px; }
  .stage { flex: 1; position: relative; display: flex; align-items: center;
           justify-content: center; background: #000; overflow: hidden; }
  .stage img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .panel { position: absolute; top: 16px; right: 16px; width: 320px;
           background: rgba(17, 24, 39, 0.85); backdrop-filter: blur(6px);
           border: 1px solid #374151; border-radius: 8px; padding: 14px;
           display: flex; flex-direction: column; gap: 12px; color: #f3f4f6; }
  .panel.collapsed > *:not(.panel-toggle) { display: none; }
  .panel.collapsed { width: auto; padding: 8px; }
  .panel-row { display: flex; justify-content: space-between; align-items: center; }
  .panel-toggle { background: transparent; color: #9ca3af; border: none;
                  font-size: 14px; cursor: pointer; padding: 4px; }
  .ctrl { display: flex; flex-direction: column; gap: 4px; }
  .ctrl-head { display: flex; justify-content: space-between; font-size: 12px;
               color: #d1d5db; }
  .ctrl-value { font-family: ui-monospace, monospace; color: #fff; }
  .ctrl-rbv { font-size: 11px; color: #9ca3af; }
  input[type=range] { width: 100%; accent-color: #3b82f6; }
  select { background: #1f2937; color: #f3f4f6; border: 1px solid #374151;
           border-radius: 4px; padding: 4px 6px; font-size: 12px; }
  .row { display: flex; gap: 8px; }
  button.btn { flex: 1; padding: 9px 10px; border-radius: 4px; border: none;
               font-size: 13px; font-weight: 600; cursor: pointer; color: #fff; }
  .btn-acq-on  { background: #10b981; }
  .btn-acq-off { background: #ef4444; }
  .btn-snap    { background: #8b5cf6; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 500; }
  .badge-on  { background: #064e3b; color: #6ee7b7; }
  .badge-init { background: #78350f; color: #fcd34d; }
  .err { background: #7f1d1d; color: #fecaca; padding: 6px 8px;
         border-radius: 4px; font-size: 12px; display: none; }
</style>
</head>
<body>
<div class="topbar">
  <div>
    <a href="/">← Dashboard</a>
    <span class="title" id="title">__CAM_ID__</span>
    <span class="title-sub" id="title-sub"></span>
    <span class="badge badge-init" id="status-badge">…</span>
  </div>
  <div>
    <a href="/cameras/__CAM_ID__/snapshot" download="__CAM_ID__-snapshot.jpg">Snapshot</a>
    &nbsp;·&nbsp;
    <a href="/cameras/__CAM_ID__/stream" target="_blank">Raw stream</a>
  </div>
</div>

<div class="stage">
  <img id="stream" src="/cameras/__CAM_ID__/stream" alt="__CAM_ID__">

  <div class="panel" id="panel">
    <div class="panel-row">
      <strong>Controls</strong>
      <button class="panel-toggle" id="panel-toggle" title="Collapse">⛶</button>
    </div>
    <div class="err" id="err"></div>

    <div class="row">
      <button class="btn btn-acq-on" id="acq-btn">…</button>
      <button class="btn btn-snap" id="snap-btn">📷 Snapshot</button>
    </div>

    <div class="ctrl">
      <div class="ctrl-head">
        <span>Exposure (s)</span>
        <span class="ctrl-value" id="exp-val">—</span>
      </div>
      <input type="range" id="exp-slider" min="0" max="0.5" step="0.001" value="0">
      <div class="panel-row">
        <span class="ctrl-rbv">RBV: <span id="exp-rbv">—</span></span>
        <select id="exp-auto">
          <option value="Off">Off / Manual</option>
          <option value="Once">Once</option>
          <option value="Continuous">Auto</option>
        </select>
      </div>
    </div>

    <div class="ctrl">
      <div class="ctrl-head">
        <span>Gain</span>
        <span class="ctrl-value" id="gain-val">—</span>
      </div>
      <input type="range" id="gain-slider" min="0" max="24" step="0.1" value="0">
      <div class="panel-row">
        <span class="ctrl-rbv">RBV: <span id="gain-rbv">—</span></span>
        <select id="gain-auto">
          <option value="Off">Off / Manual</option>
          <option value="Once">Once</option>
          <option value="Continuous">Auto</option>
        </select>
      </div>
    </div>
  </div>
</div>

<script>
const CAM_ID = "__CAM_ID__";
const $ = id => document.getElementById(id);
const fmt = (v, d = 4) => v == null ? "—" : Number(v).toFixed(d);

let dragging = { exp: false, gain: false };

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

async function refreshControl() {
  try {
    const c = await fetchJSON(`/cameras/${CAM_ID}/control`);
    $("exp-rbv").textContent = fmt(c.exposure);
    $("gain-rbv").textContent = fmt(c.gain, 2);
    if (c.exposure != null && !dragging.exp) {
      $("exp-slider").value = c.exposure;
      $("exp-val").textContent = fmt(c.exposure);
    }
    if (c.gain != null && !dragging.gain) {
      $("gain-slider").value = c.gain;
      $("gain-val").textContent = fmt(c.gain, 2);
    }
    if (c.exposure_auto && document.activeElement !== $("exp-auto")) $("exp-auto").value = c.exposure_auto;
    if (c.gain_auto    && document.activeElement !== $("gain-auto"))  $("gain-auto").value = c.gain_auto;
    const btn = $("acq-btn");
    if (c.acquire) { btn.textContent = "■ Stop acquiring"; btn.className = "btn btn-acq-off"; }
    else           { btn.textContent = "▶ Start acquiring"; btn.className = "btn btn-acq-on"; }
    hideErr();
  } catch (e) { showErr(e.message); }
}

async function refreshStatus() {
  try {
    const data = await fetchJSON("/cameras");
    const c = data.cameras.find(x => x.id === CAM_ID);
    if (!c) { showErr(`camera "${CAM_ID}" not configured`); return; }
    $("title").textContent = c.label;
    $("title-sub").textContent = `${c.id} · ${c.type} · ${c.prefix}`;
    const badge = $("status-badge");
    badge.className = "badge " + (c.status === "connected" ? "badge-on" : "badge-init");
    badge.textContent = c.status;
  } catch (e) { showErr(e.message); }
}

function showErr(msg) { const e = $("err"); e.textContent = msg; e.style.display = "block"; }
function hideErr() { $("err").style.display = "none"; }

async function putControl(body) {
  try {
    await fetchJSON(`/cameras/${CAM_ID}/control`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    refreshControl();
  } catch (e) { showErr(e.message); }
}

// slider live value display + commit on release
function bindSlider(slider, valueEl, fieldName, decimals) {
  slider.addEventListener("input", () => {
    dragging[fieldName === "exposure" ? "exp" : "gain"] = true;
    valueEl.textContent = Number(slider.value).toFixed(decimals);
  });
  slider.addEventListener("change", () => {
    putControl({ [fieldName]: Number(slider.value) }).then(() => {
      dragging[fieldName === "exposure" ? "exp" : "gain"] = false;
    });
  });
}

bindSlider($("exp-slider"),  $("exp-val"),  "exposure", 4);
bindSlider($("gain-slider"), $("gain-val"), "gain", 2);

$("exp-auto").addEventListener("change", e => putControl({ exposure_auto: e.target.value }));
$("gain-auto").addEventListener("change", e => putControl({ gain_auto: e.target.value }));

$("acq-btn").addEventListener("click", () => {
  const turningOn = $("acq-btn").textContent.includes("Start");
  putControl({ acquire: turningOn });
});

$("snap-btn").addEventListener("click", () => {
  const a = document.createElement("a");
  a.href = `/cameras/${CAM_ID}/snapshot`;
  a.download = `${CAM_ID}-${Date.now()}.jpg`;
  document.body.appendChild(a); a.click(); a.remove();
});

$("panel-toggle").addEventListener("click", () => {
  const p = $("panel");
  p.classList.toggle("collapsed");
  $("panel-toggle").textContent = p.classList.contains("collapsed") ? "▣" : "⛶";
});

refreshStatus();
refreshControl();
setInterval(refreshControl, 5000);
setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""


LANDING_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>EPICS Camera Server</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f8f9fa; margin: 0; padding: 24px; color: #1f2937; }
  h1 { margin: 0 0 4px 0; font-size: 24px; }
  .sub { color: #6b7280; font-size: 14px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 20px; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
          overflow: hidden; display: flex; flex-direction: column; }
  .card-header { display: flex; justify-content: space-between; align-items: center;
                 padding: 12px 16px; border-bottom: 1px solid #e5e7eb; }
  .card-title { font-weight: 600; font-size: 15px; }
  .card-sub { color: #6b7280; font-size: 12px; }
  .stream { width: 100%; aspect-ratio: 4/3; background: #000; object-fit: contain; display: block; }
  .row { display: flex; gap: 8px; padding: 12px 16px; }
  button, .btn { flex: 1; padding: 10px 12px; border-radius: 4px; border: none;
                 font-size: 13px; font-weight: 600; cursor: pointer; color: #fff;
                 text-align: center; text-decoration: none; }
  .btn-acq-on  { background: #10b981; }
  .btn-acq-off { background: #ef4444; }
  .btn-open    { background: #3b82f6; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px;
           font-size: 11px; font-weight: 500; }
  .badge-on  { background: #d1fae5; color: #065f46; }
  .badge-init { background: #fef3c7; color: #92400e; }
  .empty { padding: 40px; text-align: center; color: #6b7280; background: #fff; border-radius: 8px; }
  a { color: #3b82f6; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>EPICS Camera Server</h1>
<div class="sub">
  <span id="status">loading…</span> ·
  <a href="/cameras">/cameras</a> · <a href="/health">/health</a> · <a href="/docs">API docs</a>
</div>
<div id="grid" class="grid"></div>

<script>
async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

async function refresh() {
  try {
    const [health, cams] = await Promise.all([
      fetchJSON("/health"),
      fetchJSON("/cameras"),
    ]);
    document.getElementById("status").textContent =
      `${health.cameras.length} camera(s) · EPICS ${health.epics_available ? "available" : "demo"} · default ${health.default || "—"}`;
    renderGrid(cams.cameras);
  } catch (e) {
    document.getElementById("status").textContent = "error: " + e.message;
  }
}

function renderGrid(cameras) {
  const grid = document.getElementById("grid");
  if (cameras.length === 0) {
    grid.innerHTML = '<div class="empty">No cameras configured. POST /cameras to add one.</div>';
    return;
  }
  const ids = cameras.map(c => c.id);
  Array.from(grid.children).forEach(card => {
    if (!card.dataset.id || !ids.includes(card.dataset.id)) card.remove();
  });
  for (const c of cameras) {
    let card = grid.querySelector(`[data-id="${c.id}"]`);
    if (!card) card = createCard(c, grid);
    updateCardStatus(card, c);
  }
}

function createCard(c, grid) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.id = c.id;
  card.innerHTML = `
    <div class="card-header">
      <div>
        <div class="card-title">${c.label}</div>
        <div class="card-sub">${c.id} · ${c.type} · ${c.prefix}</div>
      </div>
      <span class="badge" data-role="status-badge">…</span>
    </div>
    <a href="/view/${c.id}"><img class="stream" data-role="thumb" src="/cameras/${c.id}/snapshot?t=${Date.now()}" alt="${c.id}"></a>
    <div class="row">
      <button class="btn-acq-off" data-role="acquire">…</button>
      <a class="btn btn-open" href="/view/${c.id}">Open live view ▶</a>
    </div>`;
  grid.appendChild(card);

  card.querySelector('[data-role="acquire"]').onclick = () => toggleAcquire(c.id, card);

  refreshControl(c.id, card);
  setInterval(() => refreshControl(c.id, card), 5000);
  setInterval(() => refreshThumb(c.id, card), 3000);
  return card;
}

function refreshThumb(id, card) {
  if (document.hidden) return;  // don't poll snapshots in background tabs
  const img = card.querySelector('[data-role="thumb"]');
  if (img) img.src = `/cameras/${id}/snapshot?t=${Date.now()}`;
}

function updateCardStatus(card, c) {
  const badge = card.querySelector('[data-role="status-badge"]');
  badge.className = "badge " + (c.status === "connected" ? "badge-on" : "badge-init");
  badge.textContent = c.status + (c.is_default ? " · default" : "");
}

async function refreshControl(id, card) {
  try {
    const ctrl = await fetchJSON(`/cameras/${id}/control`);
    const btn = card.querySelector('[data-role="acquire"]');
    if (ctrl.acquire) { btn.textContent = "■ Stop"; btn.className = "btn-acq-off"; }
    else              { btn.textContent = "▶ Start"; btn.className = "btn-acq-on"; }
  } catch (_) { /* IOC may be down */ }
}

async function toggleAcquire(id, card) {
  const btn = card.querySelector('[data-role="acquire"]');
  const turningOn = btn.textContent.includes("Start");
  try {
    await fetchJSON(`/cameras/${id}/control`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ acquire: turningOn }),
    });
    refreshControl(id, card);
  } catch (e) { alert("Acquire failed: " + e.message); }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


# ---------- multi-camera endpoints ----------

@app.get("/", response_class=HTMLResponse)
async def landing():
    return LANDING_PAGE


@app.get("/view/{camera_id}", response_class=HTMLResponse)
async def view(camera_id: str):
    manager.get(camera_id)  # 404 if unknown
    return VIEW_PAGE.replace("__CAM_ID__", camera_id)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "epics_available": EPICS_AVAILABLE,
        "default": manager.default,
        "cameras": [
            {"id": cid, "status": "connected" if s.is_connected() else "initializing"}
            for cid, s in manager.streams.items()
        ],
    }


@app.get("/cameras")
async def list_cameras():
    return {"default": manager.default, "cameras": manager.list_status()}


@app.post("/cameras", status_code=201)
async def add_camera(cfg: CameraConfig):
    manager.add(cfg)
    return _info_payload(manager.get(cfg.id))


@app.delete("/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    manager.remove(camera_id)
    return {"removed": camera_id, "default": manager.default}


@app.get("/cameras/{camera_id}")
async def camera_info(camera_id: str):
    return _info_payload(manager.get(camera_id))


@app.get("/cameras/{camera_id}/stream")
async def camera_stream(camera_id: str):
    return _mjpeg_response(manager.get(camera_id))


@app.get("/cameras/{camera_id}/snapshot")
async def camera_snapshot(camera_id: str):
    return _snapshot_response(manager.get(camera_id), "jpg")


@app.get("/cameras/{camera_id}/snapshot.png")
async def camera_snapshot_png(camera_id: str):
    return _snapshot_response(manager.get(camera_id), "png")


@app.get("/cameras/{camera_id}/control")
async def get_control(camera_id: str):
    stream = manager.get(camera_id)
    return await asyncio.to_thread(stream.read_control)


@app.put("/cameras/{camera_id}/control")
async def put_control(camera_id: str, update: ControlUpdate):
    stream = manager.get(camera_id)
    applied = await asyncio.to_thread(stream.set_control, update)
    rbv = await asyncio.to_thread(stream.read_control)
    return {"applied": applied, "current": rbv}


# ---------- legacy aliases (default camera) ----------

@app.get("/stream")
async def legacy_stream():
    return _mjpeg_response(manager.get_default())


@app.get("/snapshot")
async def legacy_snapshot():
    return _snapshot_response(manager.get_default(), "jpg")


@app.get("/snapshot.png")
async def legacy_snapshot_png():
    return _snapshot_response(manager.get_default(), "png")


@app.get("/info")
async def legacy_info():
    return _info_payload(manager.get_default())


if __name__ == "__main__":
    import uvicorn

    host = resolve_bind_host()
    print("EPICS Multi-Camera Server")
    print(f"Config: {CONFIG_PATH}")
    print(f"Listening on {host}:8004")
    if not EPICS_AVAILABLE:
        print("WARNING: pyepics not installed - all cameras will run in demo mode")
    uvicorn.run(app, host=host, port=8004, log_level="info")
