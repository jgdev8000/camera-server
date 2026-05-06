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
import queue
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

try:
    from epics import caget, caput
    EPICS_AVAILABLE = True
except ImportError:
    EPICS_AVAILABLE = False
    caget = caput = None

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


class ControlUpdate(BaseModel):
    exposure: Optional[float] = None
    gain: Optional[float] = None


# ---------- camera stream ----------

class CameraStream:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.frame_queue: queue.Queue = queue.Queue(maxsize=2)
        self.running = False
        self.current_frame: Optional[np.ndarray] = None
        self.lock = threading.Lock()
        self.capture_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name=f"camera-{self.cfg.id}"
        )
        self.capture_thread.start()
        logger.info("Camera %s started (prefix=%s)", self.cfg.id, self.cfg.prefix)

    def stop(self) -> None:
        self.running = False
        logger.info("Camera %s stopped", self.cfg.id)

    def _capture_loop(self) -> None:
        period = 1.0 / self.cfg.fps
        while self.running:
            try:
                frame = self._read_epics_frame() if EPICS_AVAILABLE else None
                if frame is None:
                    frame = self._demo_frame()

                frame = cv2.resize(frame, (self.cfg.width, self.cfg.height))
                with self.lock:
                    self.current_frame = frame.copy()
                try:
                    self.frame_queue.put_nowait(frame)
                except queue.Full:
                    pass

                time.sleep(period)
            except Exception as e:
                logger.error("Camera %s capture error: %s", self.cfg.id, e)
                time.sleep(1)

    def _read_epics_frame(self) -> Optional[np.ndarray]:
        try:
            data = caget(self.cfg.pv("image1:ArrayData"))
            if data is None or data.size == 0:
                return None
            h = caget(self.cfg.pv("cam1:ArraySizeY_RBV"))
            w = caget(self.cfg.pv("cam1:ArraySizeX_RBV"))
            if not (h and w and int(h) > 0 and int(w) > 0):
                return None  # IOC up but camera not acquiring yet
            if len(data.shape) == 1:
                if data.size < int(h) * int(w):
                    return None
                frame = data[: int(h) * int(w)].reshape((int(h), int(w)))
            else:
                frame = data
            if frame.dtype != np.uint8:
                frame = np.uint8(frame)
            if len(frame.shape) == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            return frame
        except Exception as e:
            logger.debug("Camera %s caget failed: %s", self.cfg.id, e)
            return None

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

    def is_connected(self) -> bool:
        return self.current_frame is not None

    # control

    def read_control(self) -> dict:
        if not EPICS_AVAILABLE:
            return {"exposure": None, "gain": None}
        return {
            "exposure": _safe_caget(self.cfg.pv("cam1:AcquireTime_RBV")),
            "gain": _safe_caget(self.cfg.pv("cam1:Gain_RBV")),
        }

    def set_control(self, update: ControlUpdate) -> dict:
        if not EPICS_AVAILABLE:
            raise HTTPException(status_code=503, detail="pyepics not available")
        applied = {}
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
        return applied


def _safe_caget(pv: str):
    try:
        v = caget(pv, timeout=1.0)
        return float(v) if v is not None else None
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
        while stream.running:
            frame = stream.get_frame()
            if frame is None:
                await asyncio.sleep(0.05)
                continue
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(buf)).encode() + b"\r\n\r\n"
                       + buf.tobytes() + b"\r\n")
            await asyncio.sleep(1.0 / stream.cfg.fps)

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
        },
    }


# ---------- multi-camera endpoints ----------

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
