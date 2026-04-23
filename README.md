[<img src="https://img.shields.io/badge/Anedya-Documentation-blue?style=for-the-badge">](https://docs.anedya.io?utm_source=github&utm_medium=link&utm_campaign=github-examples&utm_content=pi-cam)


# Pi Cam

<p align="center">
    <img src="https://cdn.anedya.io/anedya_black_banner.png" alt="Logo">
</p>


Turn a Raspberry Pi into a small CCTV-style camera system with:

- live WebRTC video streaming
- optional microphone audio
- rolling local MP4 recording
- motion detection overlay
- playback features from the peer web app/ mobile app
- Anedya-based signaling and TURN provisioning

This repository has two parts:

- `streamer/`: the Raspberry Pi device app written in Python
- `peer/`: the browser viewer web app

## Architecture Overview

At a high level, the flow is:

1. The Raspberry Pi starts capturing frames immediately.
2. The Pi records rolling MP4 segments locally.
3. The peer asks Anedya for TURN credentials.
4. The peer creates a WebRTC offer.
5. The peer writes the offer to Anedya ValueStore as `offer_<sessionId>`.
6. The Pi receives that update over Anedya MQTT.
7. The Pi creates a WebRTC answer and writes it back as `answer_<sessionId>`.
8. The peer polls for the answer, applies it, and starts receiving video/audio.
9. A WebRTC data channel is used for playback controls, seeking, and switching back to live mode.

```text
Viewer
  |
  | request TURN credentials
  | create WebRTC offer
  | write offer_<sessionId> to ValueStore
  v
Anedya Cloud
  - Platform API
  - ValueStore
  - MQTT
  - TURN relay provisioning
  |
  | notify Pi over MQTT
  v
Raspberry Pi Streamer
  - capture camera frames
  - motion detection
  - rolling recording
  - create WebRTC answer
  |
  | write answer_<sessionId> to ValueStore
  v
Viewer
  |
  | receive media
  | control playback over data channel
```

## Repository Layout

```text
.
|-- peer/
|   |-- public/
|   |   |-- index.html
|   |-- package.json
|   |-- server.js
|-- streamer/
|   |-- main.py
|   |-- pyproject.toml
|   |-- uv.lock
|-- README.md
```

## What Each Part Does

### `streamer/main.py`

The Raspberry Pi process:

- connects to Anedya over MQTT using device credentials
- captures frames from the camera using OpenCV
- records rolling 5-second MP4 segments to disk
- adds timestamp overlay to frames
- performs simple motion detection
- creates one WebRTC peer connection per viewer
- supports live mode and playback mode per viewer
- optionally captures microphone audio

### `peer/public/index.html`

The browser viewer:

- lets the user enter an Anedya Node ID
- lets the user enter an Anedya Platform API key
- lets the user force `iceTransportPolicy = "relay"` for TURN-only mode
- requests TURN credentials from Anedya
- writes offer SDP to ValueStore
- polls for answer SDP
- receives remote video/audio
- shows a playback timeline slider
- sends `timeline`, `seek`, and `live` commands over the data channel

## Requirements

### Hardware

- Raspberry Pi with network access
- camera
  - easiest path: USB/UVC webcam
  - CSI camera can work too, but camera stack configuration matters
- optional microphone for audio
- SD card and power supply

### Software

- Raspberry Pi OS or another Linux environment on the Pi
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/guides/projects/)
- an Anedya account

## Step 1: Create Your Anedya Project

Start in the Anedya Console.

1. Sign in or create an account.
2. Create a new project for this camera app.
3. Create one node for your Raspberry Pi camera and pre-authorize the node using a UUID.
     - Keep these values. You will need them later:
       - `ANEDYA_NODE_ID`
       - `ANEDYA_DEVICE_ID`
       - `ANEDYA_CONNECTION_KEY`
4. Generate a **Platform API key** for the viewer.

Official references:

- [Anedya Overview](https://docs.anedya.io/anedya-overview/)
- [Anedya Concepts](https://docs.anedya.io/essentials/concepts/)
- [Anedya Project Setup](https://docs.anedya.io/getting-started/project-setup/)
- [Anedya Device APIs](https://docs.anedya.io/device/)
- [Anedya MQTT Endpoints](https://docs.anedya.io/device/mqtt-endpoints/)
- [Anedya ValueStore Introduction](https://docs.anedya.io/features/valuestore/valuestore-intro/)
- [Anedya Platform API](https://docs.anedya.io/platform-api/)

## Step 2: Configure This Repository

This repo does not store Anedya device credentials in source code. Configure them locally before running the streamer.

### Device-side values

Create a local env file:

```bash
cp streamer/.env.example streamer/.env
```

Then edit `streamer/.env`:

- `ANEDYA_DEVICE_ID`
- `ANEDYA_NODE_ID`
- `ANEDYA_CONNECTION_KEY`
- `ANEDYA_REGION`

Those values control:

- MQTT broker hostname
- MQTT topic names
- device authentication
- which node receives signaling data

`streamer/.env` is ignored by git. Do not commit it.

### Viewer-side values

Enter your Node ID and Platform API key in the Settings panel in the UI.

## Step 3: Set Up the Raspberry Pi

Clone this repository onto the Pi and install dependencies for the Python streamer.

From the repo root:

```bash
cd streamer
uv sync
```

This will:

- create `.venv` if needed
- install Python dependencies from `pyproject.toml`
- respect `uv.lock`

## Step 4: Run the Streamer on the Pi

From `streamer/`:

```bash
uv run main.py
```

Useful options:

```bash
uv run main.py --camera 0
uv run main.py --no-audio
uv run main.py --record-path recordings
```

What to expect:

- the Pi connects to the Anedya MQTT broker for your region
- recording starts immediately
- finalized MP4 segments are written under the recording directory
- the app prints a QR payload showing the configured node and device IDs

If audio causes trouble on your Pi, use:

```bash
uv run main.py --no-audio
```

## Step 5: Run the Viewer Locally

The viewer is a static HTML page. The repo includes a tiny Express server for local development.

From `peer/`:

```bash
npm install
npm start
```

Then open:

```text
http://localhost:8080
```

The local server simply serves `peer/public/index.html`.

If you don't wish to run the viewer locally you can directly access the deployed version - [Here]().

## Step 6: Use the Viewer

When the page opens:

1. Click `Settings`.
2. Enter your Anedya Node ID.
3. Enter your Anedya Platform API key.
4. Save settings.
5. Optionally enable `Force relay/TURN only`.
6. Click `Start Stream`.

The Node ID and API key are stored in browser `localStorage` so you do not need to re-enter them every refresh.

#### Force relay/TURN only

When enabled, the browser creates the peer connection with:

```js
iceTransportPolicy: "relay"
```

That forces WebRTC to use TURN relay candidates only. This is useful for debugging difficult NAT situations or making sure the session does not fall back to direct P2P candidates.

When disabled, the browser uses:

```js
iceTransportPolicy: "all"
```

## Step 7: Verify End-to-End Flow

A successful run looks like this:

- the Pi logs show MQTT connection success
- the viewer fetches TURN credentials successfully
- the viewer writes an offer into ValueStore
- the Pi receives an `offer_<sessionId>` update
- the Pi writes `answer_<sessionId>`
- the viewer applies the answer
- viewer status changes to `Streaming`
- video appears in the page
- after the first segment finalizes, the playback slider becomes usable

## Playback Behavior

The Pi records rolling 5-second MP4 segments. The browser shows a timeline slider only.

Important detail:

- playback is available only after at least one segment is finalized
- a segment is finalized when rotation happens
- in this implementation, segments rotate every 5 seconds

That means right after startup, the stream may be live but the playback timeline can still be empty for a few seconds.

## Camera Notes

This code uses OpenCV `VideoCapture`.

That usually works best with:

- USB webcams
- UVC-compatible cameras

For Raspberry Pi CSI cameras:

- make sure the camera is detected by the OS
- make sure your camera stack is configured correctly
- test camera access independently first if capture fails

## References

### Anedya

- [Anedya Overview](https://docs.anedya.io/anedya-overview/)
- [Anedya Concepts](https://docs.anedya.io/essentials/concepts/)
- [Anedya Hardware Checklist](https://docs.anedya.io/essentials/hardware-checklist/)
- [Anedya Project Setup](https://docs.anedya.io/getting-started/project-setup/)
- [Anedya Device APIs](https://docs.anedya.io/device/)
- [Anedya MQTT Endpoints](https://docs.anedya.io/device/mqtt-endpoints/)
- [Anedya ValueStore Introduction](https://docs.anedya.io/features/valuestore/valuestore-intro/)
- [Anedya Platform API](https://docs.anedya.io/platform-api/)

### WebRTC

- [WebRTC Overview](https://webrtc.org/getting-started/overview)
- [WebRTC Peer Connections](https://webrtc.org/getting-started/peer-connections?hl=en)

### Raspberry Pi and Tooling

- [Raspberry Pi Camera Documentation](https://www.raspberrypi.com/documentation/hardware/camera/computers/camera_software.html)
- [libcamera Documentation](https://libcamera.org/docs.html)
- [uv: Working on Projects](https://docs.astral.sh/uv/guides/projects/)
