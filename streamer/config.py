"""
Centralised configuration for the Pi Cam streamer.

Reads Anedya credentials from environment variables,
defines all shared constants, sets up logging, and provides a startup
validation helper.

Environment variables required (set in streamer/.env — never commit that file):
    ANEDYA_DEVICE_ID       Device UUID from the Anedya console
    ANEDYA_NODE_ID         Node UUID from the Anedya console
    ANEDYA_CONNECTION_KEY  Device connection key from the Anedya console
    ANEDYA_REGION          API / MQTT region slug  (default: ap-in-1)
"""

import logging
import os
from pathlib import Path


# Silence low-level ICE / DTLS chatter so project logs stay readable.
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("streamer")


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a file without adding a runtime dependency.

    Keys already present in the environment are left unchanged so that
    real environment variables always take precedence over .env files.
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Load .env from the same directory as this file, then from the working
# directory (whichever is found first wins per-key, due to the guard above).
load_env_file(Path(__file__).with_name(".env"))
load_env_file(Path.cwd() / ".env")


ANEDYA_DEVICE_ID      = os.environ.get("ANEDYA_DEVICE_ID",      "")
ANEDYA_NODE_ID        = os.environ.get("ANEDYA_NODE_ID",        "")
ANEDYA_CONNECTION_KEY = os.environ.get("ANEDYA_CONNECTION_KEY", "")
ANEDYA_REGION         = os.environ.get("ANEDYA_REGION",         "ap-in-1")

MQTT_BROKER    = f"mqtt.{ANEDYA_REGION}.anedya.io"
MQTT_PORT      = 8883   # TLS port
MQTT_KEEPALIVE = 60     # seconds between keepalive pings

# Anedya Root CA 3 (ECC-256) — https://docs.anedya.io/device/mqtt-endpoints/#tls
# Embedded here so the device works without an extra file on disk.
ANEDYA_CA_CERT = """\
-----BEGIN CERTIFICATE-----
MIICDDCCAbOgAwIBAgITQxd3Dqj4u/74GrImxc0M4EbUvDAKBggqhkjOPQQDAjBL
MQswCQYDVQQGEwJJTjEQMA4GA1UECBMHR3VqYXJhdDEPMA0GA1UEChMGQW5lZHlh
MRkwFwYDVQQDExBBbmVkeWEgUm9vdCBDQSAzMB4XDTI0MDEwMTAwMDAwMFoXDTQz
MTIzMTIzNTk1OVowSzELMAkGA1UEBhMCSU4xEDAOBgNVBAgTB0d1amFyYXQxDzAN
BgNVBAoTBkFuZWR5YTEZMBcGA1UEAxMQQW5lZHlhIFJvb3QgQ0EgMzBZMBMGByqG
SM49AgEGCCqGSM49AwEHA0IABKsxf0vpbjShIOIGweak0/meIYS0AmXaujinCjFk
BFShcaf2MdMeYBPPFwz4p5I8KOCopgshSTUFRCXiiKwgYPKjdjB0MA8GA1UdEwEB
/wQFMAMBAf8wHQYDVR0OBBYEFNz1PBRXdRsYQNVsd3eYVNdRDcH4MB8GA1UdIwQY
MBaAFNz1PBRXdRsYQNVsd3eYVNdRDcH4MA4GA1UdDwEB/wQEAwIBhjARBgNVHSAE
CjAIMAYGBFUdIAAwCgYIKoZIzj0EAwIDRwAwRAIgR/rWSG8+L4XtFLces0JYS7bY
5NH1diiFk54/E5xmSaICIEYYbhvjrdR0GVLjoay6gFspiRZ7GtDDr9xF91WbsK0P
-----END CERTIFICATE-----"""

TOPIC_VALUESTORE_UPDATES = f"$anedya/device/{ANEDYA_DEVICE_ID}/valuestore/updates/json"
TOPIC_VALUESTORE_SET     = f"$anedya/device/{ANEDYA_DEVICE_ID}/valuestore/setValue/json"
TOPIC_RESPONSES          = f"$anedya/device/{ANEDYA_DEVICE_ID}/response"
TOPIC_ERRORS             = f"$anedya/device/{ANEDYA_DEVICE_ID}/errors"

AUDIO_SAMPLE_RATE   = 48000  # Hz — standard WebRTC audio sample rate
AUDIO_CHANNELS      = 1      # mono
AUDIO_FRAME_SAMPLES = 960    # 20 ms at 48 kHz — standard WebRTC frame size

# Resolution candidates tried highest-first. The driver clamps unsupported
# modes to the best available, so requesting 8K first is safe.
CAPTURE_RESOLUTION_CANDIDATES = [
    (7680, 4320),
    (3840, 2160),
    (2592, 1944),
    (2560, 1440),
    (2304, 1296),
    (1920, 1080),
    (1600, 1200),
    (1280,  720),
]

# Motion detection runs on a downscaled frame to keep CPU usage low.
MOTION_ANALYSIS_WIDTH  = 320
MOTION_ANALYSIS_HEIGHT = 240


def validate_anedya_config() -> None:
    """Raise RuntimeError early if any required credential is missing.

    Called before the event loop starts so the user gets a clear error
    message instead of a confusing MQTT authentication failure later.
    """
    missing = [
        name
        for name, value in {
            "ANEDYA_DEVICE_ID":      ANEDYA_DEVICE_ID,
            "ANEDYA_NODE_ID":        ANEDYA_NODE_ID,
            "ANEDYA_CONNECTION_KEY": ANEDYA_CONNECTION_KEY,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing required Anedya configuration: "
            + ", ".join(missing)
            + ". Set these as environment variables or create "
            + "streamer/.env from streamer/.env.example."
        )
