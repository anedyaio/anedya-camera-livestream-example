"""
Pi Cam device process — entrypoint.

Parses command-line arguments, validates Anedya credentials, prints a QR code
for easy peer pairing, then hands control to CameraStreamer which manages
recording, MQTT signaling, and WebRTC sessions for the lifetime of the process.

Usage:
    uv run streamer                        # default camera, audio on
    uv run streamer --camera 1             # alternate camera device index
    uv run streamer --no-audio             # disable microphone
    uv run streamer --record-path /tmp/rec # custom recording directory

Environment variables (or streamer/.env):
    ANEDYA_DEVICE_ID       Device UUID from the Anedya console
    ANEDYA_NODE_ID         Node UUID from the Anedya console
    ANEDYA_CONNECTION_KEY  Device connection key from the Anedya console
    ANEDYA_REGION          API region slug (default: ap-in-1)
"""

import argparse
import asyncio
import json
import logging

import qrcode

from camera_streamer import CameraStreamer
from config import ANEDYA_DEVICE_ID, ANEDYA_NODE_ID, validate_anedya_config

log = logging.getLogger("streamer")


def display_qr_code() -> None:
    """Print a QR code containing the node and device IDs.

    The peer app scans this to learn which Anedya node to connect to,
    eliminating the need to manually copy-paste UUIDs.
    """
    payload = json.dumps(
        {
            "node_id": ANEDYA_NODE_ID,
            "device_id": ANEDYA_DEVICE_ID,
        }
    )

    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)

    print("\nScan this QR to connect:\n")
    qr.print_ascii(invert=True)
    print("\nPayload:", payload, "\n")


async def main(
    camera_index: int,
    enable_audio: bool,
    enable_motion_detection: bool = False,
    record_path: str = "recordings",
) -> None:
    """Async entrypoint: run the streamer until interrupted, then shut down cleanly."""
    streamer = CameraStreamer(
        camera_index,
        enable_audio,
        record_path,
        enable_motion_detection=enable_motion_detection,
    )
    try:
        await streamer.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down...")
    finally:
        await streamer.shutdown()


def cli() -> None:
    """Synchronous console entrypoint invoked by ``uv run streamer``."""
    parser = argparse.ArgumentParser(
        description="Pi Cam WebRTC streamer (Anedya MQTT signaling)"
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        help="Camera device index (default: 0)",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Disable microphone audio track",
    )
    parser.add_argument(
        "--record-path",
        default="recordings",
        help="Directory for rolling MP4 recording segments (default: recordings)",
    )
    parser.add_argument(
        "--motion-detection",
        action="store_true",
        help="Enable OpenCV motion detection overlay/logging",
    )
    args = parser.parse_args()

    log.info(
        "Starting Pi Cam (camera=%d, audio=%s, motion=%s, record-path=%s)",
        args.camera,
        "off" if args.no_audio else "on",
        "on" if args.motion_detection else "off",
        args.record_path,
    )
    validate_anedya_config()
    display_qr_code()
    asyncio.run(
        main(
            args.camera,
            not args.no_audio,
            enable_motion_detection=args.motion_detection,
            record_path=args.record_path,
        )
    )

if __name__ == "__main__":
    cli()
