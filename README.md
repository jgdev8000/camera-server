# EPICS Camera Streaming Server

A FastAPI-based server for live video streaming from EPICS IOC USB Basler cameras with snapshot capture capabilities. Designed to work with React UI applications.

## How the Camera Server Fits in the System

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           React UI (5173)                                │
│                           Camera Tab                                     │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │ HTTP GET /stream        │
                    │ (MJPEG continuous)      │
                    └────────────┬────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Camera Server (8004)                                  │
│                    camera_server.py                                      │
│                                                                         │
│    Reads image data from EPICS PVs → Encodes as MJPEG → Streams        │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │ EPICS Channel Access    │
                    │ caget BASLER:IMAGE      │
                    └────────────┬────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Basler USB IOC (55055)                                │
│                    /opt/iocs/basler-usb/                                 │
│                                                                         │
│    Captures frames from USB camera → Publishes as EPICS PVs             │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key Points:**
- The Camera Server is a **bridge** between EPICS and HTTP
- Unlike other services, it serves continuous MJPEG video (not JSON)
- No WebSocket needed — the `<img src="/stream">` tag handles streaming
- Runs independently of the Bluesky RunEngine

For the complete system architecture, see [CODEBASE_ARCHITECTURE.md](CODEBASE_ARCHITECTURE.md).

## Overview

This server provides real-time video streaming from your EPICS-controlled Basler camera with the following features:

- **Live video streaming** to web browsers and React applications
- **Snapshot capture** with automatic download
- **H.264/H.265 encoding** support (encoder exists; current `/stream` serves MJPEG)
- **Health monitoring** with real-time server and camera status
- **CORS enabled** for seamless React integration
- **Demo mode** for testing without EPICS connection
- **Multi-threaded frame capture** for reliable performance

## Quick Start

### Prerequisites

- Python 3.7+
- FFmpeg (for H.264/H.265 encoding)
- EPICS environment (optional, demo mode available)

### Installation

1. **Install system dependencies:**
   ```bash
   sudo apt-get update
   sudo apt-get install ffmpeg python3-pip
   ```

2. **Install Python dependencies:**
   ```bash
   pip install -r /opt/camera/requirements.txt
   ```

3. **Configure EPICS environment (if needed):**
   ```bash
   export EPICS_BASE=/opt/epics
   export EPICS_CA_ADDR_LIST=your_epics_host
   ```

4. **Update EPICS PV names in `/opt/camera/camera_server.py`:**
   ```python
   EPICS_IMAGE_PV = "YOUR:IMAGE:PV"      # Image data
   EPICS_HEIGHT_PV = "YOUR:HEIGHT:PV"    # Image height
   EPICS_WIDTH_PV = "YOUR:WIDTH:PV"      # Image width
   ```

5. **Start the server:**
   ```bash
   python3 /opt/camera/camera_server.py
   ```

   Or using the startup script:
   ```bash
   bash /opt/scripts/start_camera_server.sh
   ```

The server will start on `0.0.0.0:8004` and be accessible from any machine on your network.

## Files

### Core Application

- **`camera_server.py`** - Main FastAPI application
  - `CameraStream` class: Manages frame capture from EPICS PVs
  - `H264StreamEncoder` class: Encodes frames using FFmpeg
  - REST endpoints for streaming, snapshots, and health checks

- **`CameraView.tsx`** - React component for displaying the stream
  - Live video display
  - Snapshot button
  - Server health monitoring
  - Camera settings display
  - Responsive design with Tailwind-style styling

- **`requirements_camera.txt`** - Python package dependencies

- **`start_camera_server.sh`** - Startup script with dependency checks

## Configuration

Edit `/opt/camera/camera_server.py` to customize:

```python
# EPICS Process Variables
EPICS_IMAGE_PV = "BASLER:IMAGE"      # PV name for image data
EPICS_HEIGHT_PV = "BASLER:HEIGHT"    # PV name for image height
EPICS_WIDTH_PV = "BASLER:WIDTH"      # PV name for image width

# Video Streaming Settings
STREAM_FPS = 12                       # Frames per second (10-15 recommended)
STREAM_BITRATE = "2M"                 # Video bitrate (e.g., "1M", "2M", "4M")
CODEC = "h264"                        # "h264" or "h265" for better compression

# Output Resolution
OUTPUT_WIDTH = 640                    # Width in pixels (medium resolution)
OUTPUT_HEIGHT = 480                   # Height in pixels
```

### Recommended Settings for Different Scenarios

**Low Bandwidth (Remote Access):**
```python
STREAM_FPS = 10
STREAM_BITRATE = "1M"
OUTPUT_WIDTH = 480
OUTPUT_HEIGHT = 360
```

**High Quality (Local Network):**
```python
STREAM_FPS = 30
STREAM_BITRATE = "4M"
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 960
```

**Balanced (Recommended):**
```python
STREAM_FPS = 12
STREAM_BITRATE = "2M"
OUTPUT_WIDTH = 640
OUTPUT_HEIGHT = 480
```

## API Endpoints

### Health Check
```
GET /health
```
Returns server and camera status.

**Response:**
```json
{
  "status": "healthy",
  "camera": "connected",
  "epics_available": true
}
```

### Live Stream
```
GET /stream
```
Returns MJPEG video stream compatible with HTML `<img>` tags and most browsers.

**Usage in HTML:**
```html
<img src="http://server:8004/stream" alt="Camera Stream" />
```

**Usage in React:**
```jsx
<img src={`http://server:8004/stream`} alt="Live Stream" />
```

### Snapshot (JPEG)
```
GET /snapshot
```
Returns a single JPEG image of the current frame.

**Response:** Binary JPEG image data

### Snapshot (PNG)
```
GET /snapshot.png
```
Returns a single PNG image of the current frame.

**Response:** Binary PNG image data

### Camera Info
```
GET /info
```
Returns camera configuration and server settings.

**Response:**
```json
{
  "resolution": {
    "width": 640,
    "height": 480
  },
  "fps": 12,
  "codec": "h264",
  "bitrate": "2M",
  "epics_pv": {
    "image": "BASLER:IMAGE",
    "height": "BASLER:HEIGHT",
    "width": "BASLER:WIDTH"
  }
}
```

## React Integration

### Basic Usage

1. **The React component is already in the project:**
   ```
   /opt/react-ui/src/components/CameraView.tsx
   ```

2. **Import and use in your app:**
   ```tsx
   import CameraView from './components/CameraView';

   export default function App() {
     return (
       <div>
         <h1>My EPICS Application</h1>
         <CameraView serverUrl="http://localhost:8004" />
       </div>
     );
   }
   ```

### Component Props

```typescript
interface CameraViewProps {
  serverUrl?: string;  // Default: "http://localhost:8004"
}
```

### Features

- **Live Stream Display** - Shows continuous video feed with MJPEG streaming
- **Snapshot Button** - Captures and automatically downloads images
- **Health Monitoring** - Displays real-time connection status
- **Camera Info** - Shows current resolution, FPS, codec, and bitrate
- **Server Status** - Displays EPICS and ffmpeg availability
- **Auto-Reconnect** - Health checks every 5 seconds with automatic recovery
- **Error Handling** - User-friendly error messages and status indicators

## Usage Examples

### Command Line Testing

**Check server health:**
```bash
curl http://localhost:8004/health
```

**Get current snapshot:**
```bash
curl -o snapshot.jpg http://localhost:8004/snapshot
curl -o snapshot.png http://localhost:8004/snapshot.png
```

**Get camera info:**
```bash
curl http://localhost:8004/info | python3 -m json.tool
```

### Docker Deployment

Create a `Dockerfile`:
```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements_camera.txt .
RUN pip install -r requirements_camera.txt

COPY camera_server.py .

EXPOSE 8004

CMD ["python3", "camera_server.py"]
```

Build and run:
```bash
docker build -t epics-camera-server .
docker run -p 8004:8004 -e EPICS_CA_ADDR_LIST=epics_host epics-camera-server
```

## Demo Mode

The server includes a built-in demo mode for testing without EPICS:

1. If `pyepics` is not installed, the server will generate test patterns automatically
2. Demo mode displays animated gradients with timestamp and status
3. Perfect for testing your React UI and API integration
4. Seamlessly switches to live camera when EPICS is available

To run in demo mode:
```bash
# Without installing pyepics
python3 /opt/camera/camera_server.py
```

The server will show: `⚠️ pyepics not installed - running in demo mode`

## Troubleshooting

### "ffmpeg not found" Error

Install FFmpeg:
- **Ubuntu/Debian:** `sudo apt-get install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Windows:** Download from https://ffmpeg.org/download.html

### EPICS Connection Issues

**Verify PV names:**
```bash
caget BASLER:IMAGE
caget BASLER:HEIGHT
caget BASLER:WIDTH
```

**Check EPICS environment:**
```bash
echo $EPICS_BASE
echo $EPICS_CA_ADDR_LIST
```

**Set EPICS environment:**
```bash
export EPICS_BASE=/opt/epics
export EPICS_CA_ADDR_LIST=your_epics_host:5064
```

### No Video Stream

1. Check server is running: `curl http://localhost:8004/health`
2. Verify EPICS PVs are accessible: `caget YOUR:PV:NAME`
3. Check firewall allows port 8004: `sudo ufw allow 8004`
4. Review server logs for errors

### Slow Stream / High CPU Usage

- Reduce `STREAM_FPS` (try 8-10)
- Reduce `OUTPUT_WIDTH` and `OUTPUT_HEIGHT`
- Increase `STREAM_BITRATE` if bandwidth allows
- Use `CODEC = "h265"` for better compression

### CORS Errors in Browser

The server includes CORS middleware. If you still encounter issues:

1. Verify server is running with correct URL
2. Check browser console for actual error messages
3. Ensure `serverUrl` prop matches your server address

## Performance Tuning

### Network Bandwidth

For remote/WAN access (limited bandwidth):
```python
STREAM_FPS = 8
STREAM_BITRATE = "1M"
OUTPUT_WIDTH = 480
OUTPUT_HEIGHT = 360
CODEC = "h265"  # Better compression
```

### Local Network (LAN)

For high-quality local streaming:
```python
STREAM_FPS = 24
STREAM_BITRATE = "4M"
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 960
CODEC = "h264"  # Lower CPU usage
```

### CPU Optimization

- Use `h264` codec for lower CPU usage
- Set `preset = "ultrafast"` in encoder (already configured)
- Reduce resolution if CPU usage is high

## Security Considerations

### Production Deployment

For production use, consider:

1. **Authentication:** Add authentication middleware to FastAPI
   ```python
   from fastapi.security import HTTPBearer
   security = HTTPBearer()

   @app.get("/stream")
   async def stream_video(credentials: HTTPAuthCredentials = Depends(security)):
       # Verify credentials
   ```

2. **HTTPS:** Use a reverse proxy (nginx, Apache) with SSL/TLS

3. **Rate Limiting:** Add rate limiting middleware
   ```python
   from slowapi import Limiter
   limiter = Limiter(key_func=get_remote_address)
   ```

4. **Firewall:** Restrict access to trusted networks
   ```bash
   sudo ufw allow from 192.168.1.0/24 to any port 8004
   ```

5. **EPICS:** Secure your EPICS IOC with firewall rules

## License

This project is provided as-is for use with EPICS IOCs.

## Support

For issues or feature requests:
1. Check the Troubleshooting section above
2. Review server logs: `tail -f /tmp/camera_server.log`
3. Test with demo mode first
4. Verify EPICS PVs are accessible

## Changelog

### v1.0.0 (Initial Release)
- FastAPI server with MJPEG streaming
- Snapshot capture endpoints
- Health check and info endpoints
- React component with live display
- Demo mode for testing
- H.264/H.265 encoding support ready
