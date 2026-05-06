# EPICS Multi-Camera Server

FastAPI server that streams MJPEG video and serves snapshots from one or more
EPICS Basler cameras (USB or GigE). Reads image data via `pyepics`, exposes
per-camera exposure / gain controls, and supports adding or removing cameras
at runtime via REST.

## How it fits in the system

```
┌─────────────────────────────────────────────────────────────────┐
│                       React UI (5173)                            │
│              CameraStreamer.jsx / CameraView.tsx                 │
└────────────────────────────────┬────────────────────────────────┘
                                 │ HTTP: /cameras/{id}/stream
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Camera Server (8004)                          │
│                    camera_server.py                              │
│                                                                  │
│   CameraManager → CameraStream(id) → MJPEG / JPEG / PNG          │
│                ↘                  ↘ caget/caput exposure & gain  │
└────────────────────────────────┬────────────────────────────────┘
                                 │ EPICS Channel Access
                                 ▼
                ┌────────────────┴────────────────┐
                │                                 │
        basler-usb IOC (basler1:)        basler-gige IOC (gige1:)
        /opt/iocs/basler-usb/            /opt/iocs/basler-gige/
```

## Quick start

```bash
# system deps
sudo apt-get install ffmpeg python3-pip

# python deps (in the venv at /opt/camera/camera-env)
source /opt/camera/camera-env/bin/activate
pip install -r requirements.txt

# run
python3 /opt/camera/camera_server.py
```

The server binds to whichever local interface is in `192.168.1.0/24` (currently
`192.168.1.50`) on port `8004`. Override with `HOST=0.0.0.0` or
`CAMERA_SERVER_SUBNET=10.0.0.0/24` env vars.

## Files

| File | Purpose |
|---|---|
| `camera_server.py` | FastAPI app, CameraManager, CameraStream, REST endpoints |
| `cameras.json` | Persistent list of configured cameras + default |
| `CameraStreamer.jsx` | Reference React component (camera dropdown, controls, snapshot) |
| `requirements.txt` | Python deps |

## Configuration

Cameras live in `cameras.json` next to `camera_server.py` (override with
`CAMERAS_CONFIG=/some/path.json`). Schema:

```json
{
  "default": "basler1",
  "cameras": [
    {
      "id": "basler1",
      "label": "Basler USB",
      "type": "usb",
      "prefix": "basler1:",
      "fps": 12,
      "width": 640,
      "height": 480
    },
    {
      "id": "gige1",
      "label": "Basler GigE 1 (acA1920-25gm)",
      "type": "gige",
      "prefix": "gige1:",
      "fps": 12,
      "width": 640,
      "height": 480
    }
  ]
}
```

Field rules:
- `id` — unique, `[A-Za-z0-9_-]+`. Used in URLs.
- `prefix` — must end with `:`. All EPICS PVs are derived from it.
- `type` — `usb` or `gige` (informational; both use the same Pylon driver records).
- `fps` 1–60, default 12. Capture loop polls EPICS at this rate.
- `width` / `height` — output resolution; frames are resized before encoding.

PVs derived from prefix:

| Purpose | PV |
|---|---|
| Image data | `{prefix}image1:ArrayData` |
| Width / height RBV | `{prefix}cam1:ArraySizeX_RBV` / `cam1:ArraySizeY_RBV` |
| Exposure (set / RBV) | `{prefix}cam1:AcquireTime` / `cam1:AcquireTime_RBV` |
| Gain (set / RBV) | `{prefix}cam1:Gain` / `cam1:Gain_RBV` |

`POST /cameras` and `DELETE /cameras/{id}` rewrite `cameras.json` atomically.
Editing the file by hand requires a server restart to take effect.

## API endpoints

### Multi-camera

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Server status, EPICS availability, default id, per-camera summary |
| `GET` | `/cameras` | List configured cameras with status |
| `POST` | `/cameras` | Add a camera (body = config object). 409 on duplicate id |
| `DELETE` | `/cameras/{id}` | Stop and remove a camera |
| `GET` | `/cameras/{id}` | Single-camera info (resolution, fps, prefix, derived PVs) |
| `GET` | `/cameras/{id}/stream` | MJPEG stream |
| `GET` | `/cameras/{id}/snapshot` | One JPEG frame |
| `GET` | `/cameras/{id}/snapshot.png` | One PNG frame |
| `GET` | `/cameras/{id}/control` | `{exposure, gain}` from RBV PVs |
| `PUT` | `/cameras/{id}/control` | Body `{exposure?: float, gain?: float}` → caput |

### Legacy aliases (default camera)

`/stream`, `/snapshot`, `/snapshot.png`, `/info` route to the camera named in
`cameras.json`'s `default` field. Return `503` if no default is set.

## Examples

```bash
# health & camera list
curl http://192.168.1.50:8004/health | jq
curl http://192.168.1.50:8004/cameras | jq

# read & set exposure
curl http://192.168.1.50:8004/cameras/basler1/control | jq
curl -X PUT -H 'Content-Type: application/json' \
  -d '{"exposure":0.05,"gain":1.0}' \
  http://192.168.1.50:8004/cameras/basler1/control | jq

# snapshot & stream
curl -o b1.jpg http://192.168.1.50:8004/cameras/basler1/snapshot
# open in browser:
# http://192.168.1.50:8004/cameras/gige1/stream

# add a camera at runtime
curl -X POST -H 'Content-Type: application/json' \
  -d '{"id":"gige2","label":"Basler GigE 2","type":"gige","prefix":"gige2:","fps":12,"width":640,"height":480}' \
  http://192.168.1.50:8004/cameras

# remove
curl -X DELETE http://192.168.1.50:8004/cameras/gige2
```

## React integration

The reference component is `CameraStreamer.jsx` in this repo. The active UI in
the wider system is at `/opt/react-ui/src/components/CameraView.tsx`, which
still uses the legacy single-camera endpoints — the alias routes keep it
working.

```jsx
import CameraStreamer from './CameraStreamer';

export default function App() {
  return <CameraStreamer serverUrl="http://192.168.1.50:8004" />;
}
```

The component fetches `/cameras` to populate a dropdown, streams from the
selected camera, polls `/control` every 5 s, and writes new exposure/gain
values via `PUT /control`.

## Demo mode

If `pyepics` is missing or no IOC is reachable, each `CameraStream` renders a
gradient test pattern with the camera id printed on it. The server still
serves snapshots and streams; control endpoints return `null` for RBV and
reject `PUT` with `503`.

## Troubleshooting

- **No frames** — check the IOC: `caget basler1:image1:ArrayData` (or whichever prefix). Server logs `Camera <id> caget failed` at DEBUG level.
- **Bind error** — server picks the local IP on `CAMERA_SERVER_SUBNET` (default `192.168.1.0/24`). If your interface is elsewhere, set `HOST=0.0.0.0` or change the subnet env var.
- **Add returns 409** — id is already in use; pick a different one or `DELETE` first.
- **GigE specifically** — make sure jumbo frames are enabled on the NIC (`sudo ip link set <iface> mtu 9000`) and socket buffers are bumped (`sudo sysctl -w net.core.rmem_max=33554432`).
