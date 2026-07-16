# WebSocket Protocol

The server exposes two WebSocket endpoints:

| Endpoint | Purpose |
|---|---|
| `WS /stream` | Sensor data ingestion — Pi (or any compatible device) streams data here |
| `WS /ws` | Live feed subscription — clients receive processed sensor data in real time |

Base URL (local): `ws://192.168.0.182:8080`

---

## `/stream` — Sensor ingestion

The Pi connects here and sends one JSON frame per message. Each frame has a `type` field and a Unix timestamp `ts`.

### IMU frame

Sent at **50 Hz**. Raw integer counts from an MPU6050 at default range (±2g / ±250°/s).

```json
{
  "type": "imu",
  "ts": 1783200000.123,
  "accel_x": 512,
  "accel_y": -128,
  "accel_z": 16200,
  "gyro_x": 45,
  "gyro_y": -12,
  "gyro_z": 8
}
```

| Field | Type | Unit | Scale factor |
|---|---|---|---|
| `accel_x/y/z` | integer | raw LSB | ÷ 16384.0 → g |
| `gyro_x/y/z` | integer | raw LSB | ÷ 131.0 → °/s |

At rest on a flat surface, `accel_z` ≈ ±16384 (1g). Sign depends on sensor orientation.

### Audio frame

Sent at **50 Hz**. RMS amplitude from a single-channel 48 kHz audio stream.

```json
{
  "type": "audio",
  "ts": 1783200000.123,
  "spl": 0.0412
}
```

| Field | Type | Range | Notes |
|---|---|---|---|
| `spl` | float | 0.0 – ~0.5 | RMS amplitude (not calibrated in Pa) |

### Camera frame

Sent at **30 Hz** (configurable via `PI_CAMERA_HZ`). Base64-encoded JPEG.

```json
{
  "type": "camera",
  "ts": 1783200000.123,
  "jpeg": "<base64-encoded JPEG>"
}
```

| Field | Notes |
|---|---|
| `jpeg` | Standard base64 (RFC 4648). JPEG encoded from a **BGR** frame — R and B channels are swapped relative to standard RGB JPEG. The server corrects this before broadcasting to `/ws` clients. |
| Resolution | 320 × 240 px (default) |
| Quality | JPEG quality 50 (default) |

### Commands sent from server → Pi

The server may push commands over the same `/stream` connection:

```json
{ "cmd": "start_wav" }
{ "cmd": "stop_wav" }
```

`start_wav` / `stop_wav` tell the Pi to begin/end WAV recording. The finished WAV is uploaded by the Pi to `POST /wav`.

---

## `/ws` — Live feed subscription

Clients connect here to receive processed sensor data. Messages are either **JSON text** or **binary**.

### JSON messages (text frames)

#### `status` — Pi connection state

Sent when the Pi connects or disconnects, and once on initial connection.

```json
{ "t": "status", "connected": true }
```

#### `rec_state` — Recording state

Sent when recording starts or stops, and once on initial connection.

```json
{ "t": "rec_state", "recording": true, "exercise_id": 13 }
{ "t": "rec_state", "recording": false }
```

#### `imu` — Accelerometer + gyroscope

Forwarded at Pi rate (~50 Hz). Values are already converted to physical units.

```json
{
  "t": "imu",
  "ts": 1783200000.123,
  "ax": 0.031,  "ay": -0.008, "az": 0.998,
  "gx": 0.344,  "gy": -0.092, "gz": 0.061
}
```

| Field | Unit |
|---|---|
| `ax/ay/az` | g (1g ≈ 9.81 m/s²) |
| `gx/gy/gz` | °/s |

#### `audio` — Sound pressure level

Forwarded at Pi rate (~50 Hz).

```json
{ "t": "audio", "ts": 1783200000.123, "spl": 0.0412 }
```

#### `mar` — Mouth opening (vertical + horizontal)

Computed from camera frames via MediaPipe Face Landmarker. Rate is limited by inference time (~8–10 Hz on server hardware).

```json
{
  "t": "mar",
  "ts": 1783200000.123,
  "vertical": 0.0131,
  "horizontal": 0.358
}
```

| Field | Range | Description |
|---|---|---|
| `vertical` | 0.0 – ~0.15 | `\|y_upper_lip − y_lower_lip\|` normalised to frame height |
| `horizontal` | 0.1 – ~0.5 | `\|x_left_corner − x_right_corner\|` normalised to frame width |

MAR ratio = `vertical / horizontal`. Closed mouth ≈ 0.03–0.10; open mouth ≈ 0.3+.

### Binary messages

#### Camera frame — `CAM:` prefix

```
Bytes 0–3:  b"CAM:"  (4 bytes, ASCII)
Bytes 4–:   JPEG image data
```

The JPEG has been color-corrected (BGR → RGB) and optionally rotated by the server before forwarding. It can be displayed directly in a browser `<img>` element via a Blob URL.

#### Recording download — `ZIP:` prefix

Sent to the subscribing client that triggered stop via a WebSocket `stop_rec` command (legacy live-dashboard flow).

```
Bytes 0–3:    b"ZIP:"        (4 bytes)
Bytes 4–67:   filename       (64 bytes, zero-padded UTF-8)
Bytes 68–:    ZIP file data
```

---

## Connecting — minimal example (JavaScript)

```js
const ws = new WebSocket('ws://192.168.0.182:8080/ws');
ws.binaryType = 'arraybuffer';

ws.onmessage = (event) => {
  if (event.data instanceof ArrayBuffer) {
    const header = String.fromCharCode(...new Uint8Array(event.data, 0, 4));
    if (header === 'CAM:') {
      const blob = new Blob([event.data.slice(4)], { type: 'image/jpeg' });
      document.getElementById('camera').src = URL.createObjectURL(blob);
    }
    return;
  }

  const msg = JSON.parse(event.data);
  if (msg.t === 'imu')    console.log('accel z:', msg.az, 'g');
  if (msg.t === 'audio')  console.log('spl:', msg.spl);
  if (msg.t === 'mar')    console.log('MAR:', msg.vertical / msg.horizontal);
};
```

## Connecting — minimal example (Python)

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect('ws://192.168.0.182:8080/ws') as ws:
        async for raw in ws:
            if isinstance(raw, bytes):
                if raw[:4] == b'CAM:':
                    jpeg = raw[4:]  # JPEG bytes, display or save
                continue
            msg = json.loads(raw)
            if msg['t'] == 'imu':
                print(f"ax={msg['ax']:.3f}g  gz={msg['gz']:.1f}°/s")
            elif msg['t'] == 'mar':
                print(f"MAR={msg['vertical']/msg['horizontal']:.3f}")

asyncio.run(main())
```

---

## Notes

- **Timestamps** (`ts`) are Unix epoch seconds (float) from the Pi's clock. The Pi should be NTP-synced; the server logs a warning if lag exceeds 2 seconds.
- **Only one Pi** can be connected to `/stream` at a time. A second connection attempt is rejected with close code 4000.
- **Multiple clients** can subscribe to `/ws` simultaneously.
- The server applies a **90° CW rotation** to camera frames when `CAMERA_ROTATE=90CW` is set (corrects for sideways camera mounting). Other values: `90CCW`, `180`.
