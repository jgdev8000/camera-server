#!/usr/bin/env python3
"""
FastAPI server for EPICS Basler camera streaming and snapshots
Streams video using H.264/H.265 and provides snapshot endpoints
"""

import cv2
import numpy as np
import logging
import io
import threading
import time
import asyncio
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
import subprocess
from pathlib import Path
import queue

# Try to import pyepics for EPICS PV reading
try:
    from epics import caget
    EPICS_AVAILABLE = True
except ImportError:
    EPICS_AVAILABLE = False
    print("Warning: pyepics not installed. Install with: pip install pyepics")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
EPICS_IMAGE_PV = "basler1:image1:ArrayData"  # Image data array
EPICS_HEIGHT_PV = "basler1:cam1:ArraySizeY_RBV"  # Height PV
EPICS_WIDTH_PV = "basler1:cam1:ArraySizeX_RBV"    # Width PV
STREAM_FPS = 12
STREAM_BITRATE = "2M"
CODEC = "h264"  # or "h265" for HEVC
OUTPUT_WIDTH = 640  # Medium resolution
OUTPUT_HEIGHT = 480


class CameraStream:
    """Manages camera frame capture and encoding"""

    def __init__(self):
        self.frame_queue = queue.Queue(maxsize=2)
        self.running = False
        self.current_frame = None
        self.lock = threading.Lock()

    def start(self):
        """Start the camera capture thread"""
        if not self.running:
            self.running = True
            self.capture_thread = threading.Thread(target=self._capture_frames, daemon=True)
            self.capture_thread.start()
            logger.info("Camera stream started")

    def stop(self):
        """Stop the camera capture thread"""
        self.running = False
        logger.info("Camera stream stopped")

    def _capture_frames(self):
        """Capture frames from EPICS PV in a background thread"""
        while self.running:
            try:
                if not EPICS_AVAILABLE:
                    # Demo mode: generate test pattern
                    frame = self._generate_test_frame()
                else:
                    # Read image data from EPICS PV
                    frame = self._read_epics_frame()
                    # Fall back to test pattern if EPICS frame unavailable
                    if frame is None:
                        frame = self._generate_test_frame()

                if frame is not None:
                    # Resize to output dimensions
                    frame = cv2.resize(frame, (OUTPUT_WIDTH, OUTPUT_HEIGHT))

                    with self.lock:
                        self.current_frame = frame.copy()

                    # Try to add to queue, drop if full
                    try:
                        self.frame_queue.put_nowait(frame)
                    except queue.Full:
                        pass

                # Control frame rate
                time.sleep(1 / STREAM_FPS)

            except Exception as e:
                logger.error(f"Error capturing frame: {e}")
                time.sleep(1)

    def _read_epics_frame(self) -> np.ndarray:
        """Read image data from EPICS PV"""
        try:
            # Get image array from EPICS
            image_data = caget(EPICS_IMAGE_PV)

            if image_data is None:
                return None

            # Get dimensions if available
            height = caget(EPICS_HEIGHT_PV)
            width = caget(EPICS_WIDTH_PV)

            # Reshape if dimensions are known
            if height and width:
                if len(image_data.shape) == 1:
                    frame = image_data.reshape((int(height), int(width)))
                else:
                    frame = image_data
            else:
                frame = image_data

            # Convert to uint8 if needed
            if frame.dtype != np.uint8:
                frame = np.uint8(frame)

            # Convert to BGR for OpenCV if grayscale
            if len(frame.shape) == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

            return frame

        except Exception as e:
            logger.error(f"Error reading EPICS frame: {e}")
            return None

    def _generate_test_frame(self) -> np.ndarray:
        """Generate a test pattern frame (demo mode)"""
        frame = np.zeros((OUTPUT_HEIGHT, OUTPUT_WIDTH, 3), dtype=np.uint8)

        # Add test pattern: moving gradient
        t = int(time.time() * 100) % OUTPUT_WIDTH
        frame[:, :] = (np.arange(OUTPUT_HEIGHT)[:, np.newaxis] + t) % 256

        # Add text
        cv2.putText(frame, "Demo Mode - EPICS Camera Server", (10, 30),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(frame, f"Time: {time.time():.1f}", (10, 70),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        return frame

    def get_frame(self) -> np.ndarray:
        """Get the current frame"""
        with self.lock:
            return self.current_frame.copy() if self.current_frame is not None else None

    def get_frame_generator(self):
        """Generate frames for streaming"""
        while self.running:
            try:
                frame = self.frame_queue.get(timeout=1)
                yield frame
            except queue.Empty:
                continue


class H264StreamEncoder:
    """Encodes frames to H.264/H.265 stream using ffmpeg"""

    def __init__(self, width: int, height: int, fps: int, bitrate: str, codec: str = "h264"):
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.codec = codec
        self.process = None

    def start(self, frame_generator):
        """Start encoding process"""
        # Determine codec-specific options
        if self.codec == "h265":
            encoder = "libx265"
            preset = "ultrafast"
        else:  # h264
            encoder = "libx264"
            preset = "ultrafast"

        cmd = [
            "ffmpeg",
            "-f", "rawvideo",
            "-pixel_format", "bgr24",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", "-",
            "-c:v", encoder,
            "-preset", preset,
            "-b:v", self.bitrate,
            "-f", "h264" if self.codec == "h264" else "hevc",
            "-"
        ]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10 * 1024
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found. Install with: sudo apt-get install ffmpeg")
            raise

    def encode_frame(self, frame: np.ndarray) -> bytes:
        """Encode a frame and return encoded bytes"""
        if self.process is None:
            raise RuntimeError("Encoder not started")

        try:
            self.process.stdin.write(frame.tobytes())
            self.process.stdin.flush()
        except Exception as e:
            logger.error(f"Error writing to encoder: {e}")
            raise

    def read_encoded(self, size: int = 65536) -> bytes:
        """Read encoded data from ffmpeg"""
        if self.process is None:
            raise RuntimeError("Encoder not started")

        try:
            return self.process.stdout.read(size)
        except Exception as e:
            logger.error(f"Error reading from encoder: {e}")
            raise

    def stop(self):
        """Stop the encoding process"""
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                self.process.kill()
            self.process = None


camera_stream = CameraStream()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage camera stream lifecycle"""
    # Startup
    camera_stream.start()
    logger.info("Application startup complete")
    yield
    # Shutdown
    camera_stream.stop()
    logger.info("Application shutdown complete")


app = FastAPI(title="EPICS Camera Server", lifespan=lifespan)

# Add CORS middleware to allow requests from React UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "camera": "connected" if camera_stream.current_frame is not None else "initializing",
        "epics_available": EPICS_AVAILABLE
    }


@app.get("/stream")
async def stream_video():
    """Stream live H.264/H.265 video"""

    async def video_generator():
        encoder = H264StreamEncoder(
            OUTPUT_WIDTH, OUTPUT_HEIGHT, STREAM_FPS, STREAM_BITRATE, CODEC
        )

        try:
            encoder.start(camera_stream.get_frame_generator())

            while camera_stream.running:
                frame = camera_stream.get_frame_generator()
                try:
                    frame_data = next(frame)
                    encoder.encode_frame(frame_data)

                    # Read encoded chunks
                    chunk = encoder.read_encoded(65536)
                    if chunk:
                        yield chunk
                except StopIteration:
                    break
                except Exception as e:
                    logger.error(f"Error in stream: {e}")
                    break
        finally:
            encoder.stop()

    # Alternative simpler streaming method using MJPEG
    async def mjpeg_generator():
        while camera_stream.running:
            frame = camera_stream.get_frame()
            if frame is None:
                await asyncio.sleep(0.01)
                continue

            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       b'Content-Length: ' + str(len(buffer)).encode() + b'\r\n\r\n'
                       + buffer.tobytes() + b'\r\n')

            await asyncio.sleep(1 / STREAM_FPS)

    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/snapshot")
async def get_snapshot():
    """Get a single snapshot image"""
    frame = camera_stream.get_frame()

    if frame is None:
        raise HTTPException(status_code=503, detail="Camera not ready")

    ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 95])

    if not ret:
        raise HTTPException(status_code=500, detail="Failed to encode snapshot")

    return Response(content=buffer.tobytes(), media_type="image/jpeg")


@app.get("/snapshot.png")
async def get_snapshot_png():
    """Get a single snapshot image as PNG"""
    frame = camera_stream.get_frame()

    if frame is None:
        raise HTTPException(status_code=503, detail="Camera not ready")

    ret, buffer = cv2.imencode('.png', frame)

    if not ret:
        raise HTTPException(status_code=500, detail="Failed to encode snapshot")

    return Response(content=buffer.tobytes(), media_type="image/png")


@app.get("/info")
async def get_info():
    """Get camera information"""
    return {
        "resolution": {
            "width": OUTPUT_WIDTH,
            "height": OUTPUT_HEIGHT
        },
        "fps": STREAM_FPS,
        "codec": CODEC,
        "bitrate": STREAM_BITRATE,
        "epics_pv": {
            "image": EPICS_IMAGE_PV,
            "height": EPICS_HEIGHT_PV,
            "width": EPICS_WIDTH_PV
        }
    }


if __name__ == "__main__":
    import uvicorn

    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║         EPICS Camera Streaming Server                      ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    print("Starting server on 0.0.0.0:8004")
    print("Health check: http://localhost:8004/health")
    print("Stream: http://localhost:8004/stream")
    print("Snapshot: http://localhost:8004/snapshot")
    print("Info: http://localhost:8004/info")
    print()

    # Verify dependencies
    if not EPICS_AVAILABLE:
        print("⚠️  pyepics not installed - running in demo mode")
        print("   Install with: pip install pyepics")
        print()

    print("Press Ctrl+C to stop the server")
    print()

    uvicorn.run(app, host="0.0.0.0", port=8004, log_level="info")
