"""
Unified camera streamer — entrypoint.

Supports both USB and RTSP camera sources with Anedya Commands signaling.
Selection is done via environment variables (or .env file):

    CAMERA_SOURCE      "usb" or "rtsp"       (default: usb)
    CAMERA_SOURCE_URL  RTSP URL              (required when CAMERA_SOURCE=rtsp)

Usage:
    uv run streamer                        # USB camera, commands signaling
    uv run streamer --camera 1             # alternate camera device index
    uv run streamer --no-audio             # disable microphone
    uv run streamer --record-path /tmp/rec # custom recording directory

Environment variables (or unified-streamer/.env):
    ANEDYA_DEVICE_ID       Device UUID from the Anedya console
    ANEDYA_CONNECTION_KEY  Device connection key from the Anedya console
    ANEDYA_REGION          API region slug (default: ap-in-1)
"""

import argparse
import asyncio
import logging

from camera_streamer import CameraStreamer
from config import (
    CAMERA_SOURCE,
    CAMERA_SOURCE_URL,
    validate_anedya_config,
)

log = logging.getLogger("streamer")


async def main(
    camera_index: int,
    source_mode: str,
    source_url: str,
    enable_audio: bool,
    enable_motion_detection: bool = False,
    record_path: str = "recordings",
) -> None:
    """Async entrypoint: run the streamer until interrupted, then shut down cleanly."""
    streamer = CameraStreamer(
        camera_index,
        source_mode=source_mode,
        source_url=source_url,
        enable_audio=enable_audio,
        record_path=record_path,
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
        description="Unified WebRTC streamer (USB + RTSP, Commands Signaling)"
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        help="Camera device index for USB mode (default: 0)",
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
        "Starting streamer (source=%s, signaling=COMMANDS, camera=%d, audio=%s, motion=%s, record-path=%s)",
        CAMERA_SOURCE.upper(),
        args.camera,
        "off" if args.no_audio else "on",
        "on" if args.motion_detection else "off",
        args.record_path,
    )
    validate_anedya_config()

    # Run the main async function
    asyncio.run(
        main(
            args.camera,
            source_mode=CAMERA_SOURCE,
            source_url=CAMERA_SOURCE_URL,
            enable_audio=not args.no_audio,
            enable_motion_detection=args.motion_detection,
            record_path=args.record_path,
        )
    )

if __name__ == "__main__":
    cli()
