#!/usr/bin/env python3
"""
spotify_automation.py

Policy-safe Spotify playback controller using the official Web API.
- Uses a REFRESH TOKEN to obtain short-lived ACCESS TOKENS.
- Can transfer playback to a device, start playback for a context/URIs,
  toggle shuffle, set volume, and run a timed session.
- Handles 429/5xx with exponential backoff + jitter.

USAGE (examples):
  # Start a 30 min playlist session (Premium + consent required)
  python spotify_automation.py --context-uri spotify:playlist:37i9dQZF1DXcBWIGoYBM5M --duration 1800

  # Transfer playback to a specific device and enable shuffle
  python spotify_automation.py --device-id <DEVICE_ID> --shuffle on --context-uri spotify:album:...

  # Pass multiple track URIs (comma-separated)
  python spotify_automation.py --uris spotify:track:AAA,spotify:track:BBB --duration 900

ENV VARS (required):
  SPOTIFY_CLIENT_ID           -> Your app's Client ID
  SPOTIFY_CLIENT_SECRET       -> Your app's Client Secret (for confidential Authorization Code flow)
  SPOTIFY_REFRESH_TOKEN       -> Refresh token obtained after initial OAuth consent
  (optional) SPOTIFY_REDIRECT_URI -> Used during your initial auth flow (not needed for runtime)

NOTES & COMPLIANCE:
  - Playback control requires Spotify Premium on the target account.
  - Only use with accounts you own/manage and with explicit user consent.
  - Do NOT use this script to inflate streams or manipulate metrics.
"""

import argparse
import os
import sys
import time
import json
import random
import signal
import logging
from typing import Any, Dict, Optional, Tuple, List
import base64

import requests

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

log = logging.getLogger("spotify_automation")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

# --------------- Utility: Exponential Backoff with Jitter ---------------

def sleep_backoff(attempt: int, base_ms: int = 500, factor: float = 2.0, max_ms: int = 20000) -> float:
    """Return a sleep time (seconds) for current attempt with full jitter."""
    ms = min(int(base_ms * (factor ** (attempt - 1))), max_ms)
    jitter = random.randint(0, ms)
    secs = (ms + jitter) / 1000.0
    return secs

def req(
    method: str,
    url: str,
    headers: Dict[str, str],
    expected: Tuple[int, ...] = (200, 201, 202, 204),
    max_retries: int = 6,
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    """HTTP request wrapper with retry/backoff on 429/5xx. Raises for non-expected codes."""
    attempt = 0
    while True:
        attempt += 1
        resp = requests.request(method, url, headers=headers, json=json_body, params=params, timeout=30)
        if resp.status_code in expected:
            return resp

        # Handle rate limit (429) with Retry-After
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else sleep_backoff(attempt)
            log.warning(f"429 Too Many Requests. Waiting {wait:.2f}s before retry...")
            time.sleep(wait)
            if attempt <= max_retries:
                continue
            else:
                break

        # Handle 5xx with backoff
        if 500 <= resp.status_code < 600 and attempt <= max_retries:
            wait = sleep_backoff(attempt)
            log.warning(f"{resp.status_code} Server error. Retrying in {wait:.2f}s ...")
            time.sleep(wait)
            continue

        # Otherwise, give up
        break

    # If here, error
    try:
        data = resp.json()
    except Exception:
        data = {"text": resp.text}
    raise RuntimeError(f"HTTP {resp.status_code} error for {url}: {json.dumps(data)[:500]}")

# --------------- Auth (Refresh Token) ---------------

def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """
    Exchange a refresh token for a short-lived access token.
    Uses the Authorization Code (confidential client) refresh flow.
    """
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to refresh token: {r.status_code} {r.text}")
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError(f"No access_token in response: {r.text}")
    return tok

# --------------- Spotify Player Helpers ---------------

def get_devices(token: str) -> List[Dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    r = req("GET", f"{API_BASE}/me/player/devices", headers=headers)
    return r.json().get("devices", [])

def transfer_playback(token: str, device_id: str, play: bool = True) -> None:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"device_ids": [device_id], "play": play}
    req("PUT", f"{API_BASE}/me/player", headers=headers, json_body=body, expected=(204,))

def set_shuffle(token: str, device_id: str, state: bool) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    params = {"state": str(state).lower(), "device_id": device_id}
    req("PUT", f"{API_BASE}/me/player/shuffle", headers=headers, params=params, expected=(204,))

def set_volume(token: str, device_id: str, volume_percent: int) -> None:
    volume_percent = max(0, min(volume_percent, 100))
    headers = {"Authorization": f"Bearer {token}"}
    params = {"volume_percent": volume_percent, "device_id": device_id}
    req("PUT", f"{API_BASE}/me/player/volume", headers=headers, params=params, expected=(204,))

def start_playback(
    token: str,
    device_id: Optional[str] = None,
    context_uri: Optional[str] = None,
    uris: Optional[List[str]] = None,
    position_ms: int = 0,
) -> None:
    """
    Start or resume playback.
    Provide either context_uri (playlist/album/artist) OR a list of track URIs.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body: Dict[str, Any] = {"position_ms": position_ms}
    if context_uri:
        body["context_uri"] = context_uri
    if uris:
        body["uris"] = uris

    params = {}
    if device_id:
        params["device_id"] = device_id

    req("PUT", f"{API_BASE}/me/player/play", headers=headers, json_body=body, params=params, expected=(204,))

def pause_playback(token: str, device_id: Optional[str] = None) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    params = {"device_id": device_id} if device_id else None
    req("PUT", f"{API_BASE}/me/player/pause", headers=headers, params=params, expected=(204,))

# --------------- Runner ---------------

stop_flag = False
def _sigint_handler(signum, frame):
    global stop_flag
    stop_flag = True
    log.info("Received stop signal. Gracefully finishing session...")

signal.signal(signal.SIGINT, _sigint_handler)
signal.signal(signal.SIGTERM, _sigint_handler)

def run_session(
    token: str,
    duration_sec: int,
    device_id: Optional[str],
    context_uri: Optional[str],
    uris: Optional[List[str]],
    shuffle: Optional[bool],
    volume: Optional[int],
    transfer: bool,
) -> None:
    # Optionally transfer playback
    if transfer and device_id:
        log.info(f"Transferring playback to device: {device_id}")
        try:
            transfer_playback(token, device_id, play=False)
        except Exception as e:
            log.warning(f"Transfer failed (will continue): {e}")

    # Optional settings
    if shuffle is not None and device_id:
        log.info(f"Setting shuffle = {shuffle}")
        try:
            set_shuffle(token, device_id, shuffle)
        except Exception as e:
            log.warning(f"Shuffle failed: {e}")

    if volume is not None and device_id:
        log.info(f"Setting volume = {volume}%")
        try:
            set_volume(token, device_id, volume)
        except Exception as e:
            log.warning(f"Volume set failed: {e}")

    # Start playback
    log.info("Starting playback...")
    start_playback(token, device_id=device_id, context_uri=context_uri, uris=uris)

    # Timed session with small jitter pauses
    end_ts = time.time() + duration_sec
    while not stop_flag and time.time() < end_ts:
        time.sleep(random.uniform(5.0, 12.0))

    # Pause at the end
    try:
        pause_playback(token, device_id=device_id)
        log.info("Session ended. Paused playback.")
    except Exception as e:
        log.warning(f"Pause failed: {e}")

# --------------- CLI ---------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Policy-safe Spotify playback controller (Web API)")
    p.add_argument("--device-id", help="Target device ID (see --list-devices)")
    p.add_argument("--list-devices", action="store_true", help="List available devices and exit")
    p.add_argument("--context-uri", help="Context URI (playlist/album/artist), e.g., spotify:playlist:...")
    p.add_argument("--uris", help="Comma-separated track URIs")
    p.add_argument("--duration", type=int, default=1800, help="Session duration in seconds (default: 1800)")
    p.add_argument("--shuffle", choices=["on", "off"], help="Enable/disable shuffle (requires device-id)")
    p.add_argument("--volume", type=int, help="Set volume percent (0-100; requires device-id)")
    p.add_argument("--transfer", action="store_true", help="Transfer playback to device before starting")
    return p.parse_args()

def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        log.error(f"Missing required env var: {name}")
        sys.exit(2)
    return val

def main() -> None:
    args = parse_args()

    client_id = require_env("SPOTIFY_CLIENT_ID")
    client_secret = require_env("SPOTIFY_CLIENT_SECRET")
    refresh_token = require_env("SPOTIFY_REFRESH_TOKEN")

    # Token
    token = get_access_token(client_id, client_secret, refresh_token)
    log.info("Access token obtained.")

    # List devices
    if args.list_devices:
        devices = get_devices(token)
        if not devices:
            print("No active devices found. Open Spotify on a device and try again.")
        else:
            print(json.dumps(devices, indent=2))
        return

    # Parse URIs
    uris = [u.strip() for u in args.uris.split(",")] if args.uris else None
    context_uri = args.context_uri

    if not (context_uri or uris):
        log.error("Provide either --context-uri or --uris (comma-separated). See --help.")
        sys.exit(2)

    shuffle = None
    if args.shuffle is not None:
        shuffle = True if args.shuffle == "on" else False

    run_session(
        token=token,
        duration_sec=args.duration,
        device_id=args.device_id,
        context_uri=context_uri,
        uris=uris,
        shuffle=shuffle,
        volume=args.volume,
        transfer=args.transfer,
    )

if __name__ == "__main__":
    main()
