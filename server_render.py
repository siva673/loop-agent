# server.py
# -----------------------------------------------------------------------------
# Spotify "Loop Agent" — reliable loop playback from simple commands.
#
# Examples (POST /play with JSON):
#   { "command": "play \"Levitating - Dua Lipa\" \"Calm Down - Rema\" in loop till 10 minutes on iPhone" }
#
# Cloud-ready:
#   - Adds /login and /callback for Spotify OAuth on Render/any server.
#   - Disables browser popups during OAuth (open_browser=False).
#   - Keeps your robust iOS start logic (pause -> transfer -> start -> verify).
#
# Endpoints
#   GET  /ping       -> quick liveness check
#   GET  /devices    -> Spotify Connect devices
#   GET  /status     -> current playback payload
#   GET  /login      -> begin Spotify OAuth (server)
#   GET  /callback   -> OAuth redirect target (server)
#   POST /play       -> start loop until stop time/duration
#
# Requirements (.env or Render Environment):
#   SPOTIFY_CLIENT_ID=...
#   SPOTIFY_CLIENT_SECRET=...
#   SPOTIFY_REDIRECT_URI=https://<your-service>.onrender.com/callback
#   (optional) DEFAULT_DEVICE_NAME=iPhone
# -----------------------------------------------------------------------------

import os
import re
import time
import threading
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

from dateutil import tz
from dateutil.parser import parse as dtparse
from flask import Flask, request, jsonify, redirect

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Load .env locally (safe if not present on cloud)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Scopes: read playback, control playback, and manage a private playlist
SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "playlist-modify-private "
    "playlist-read-private"
)

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Spotify client & OAuth helpers
# -----------------------------------------------------------------------------

def sp_client() -> spotipy.Spotify:
    """Create an authenticated Spotify client.
    On servers, we cannot pop a browser, so open_browser=False.
    Token is cached in .cache-loop-agent.
    """
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=os.environ["SPOTIFY_CLIENT_ID"],
            client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=os.environ.get(
                "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback"
            ),
            scope=SCOPES,
            cache_path=".cache-loop-agent",
            open_browser=False,
            requests_timeout=30,
        )
    )

def oauth_manager() -> SpotifyOAuth:
    """Convenience to build a SpotifyOAuth manager identical to sp_client()."""
    return SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ.get(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback"
        ),
        scope=SCOPES,
        cache_path=".cache-loop-agent",
        open_browser=False,
    )

# -----------------------------------------------------------------------------
# Command parsing & time resolution
# -----------------------------------------------------------------------------

def parse_cmd(cmd: str) -> Tuple[List[str], Optional[str], Optional[str]]:
    """
    Flexible pattern:
      play "A" ["B" ...] in loop till <time-or-duration> [on <device>]

    Examples:
      play "Levitating - Dua Lipa" "Calm Down - Rema" in loop till 10 minutes on iPhone
      play "Believer - Imagine Dragons" in loop till 10:45 pm
    """
    titles = re.findall(r'"([^"]+)"', cmd)
    if not titles:
        raise ValueError('Use quotes: play "Song A" "Song B" in loop till 10 minutes [on iPhone]')

    m_till = re.search(r'\btill (.+?)(?: on |$)', cmd, flags=re.IGNORECASE)
    until_txt = m_till.group(1).strip() if m_till else None

    m_dev = re.search(r'\bon (.+)$', cmd, flags=re.IGNORECASE)
    device_name = m_dev.group(1).strip() if m_dev else None

    return titles, until_txt, device_name

def resolve_until_today(until_text: Optional[str]) -> datetime:
    """Convert '15 minutes' or '10:30 pm' into an absolute timestamp (local tz)."""
    now = datetime.now(tz.tzlocal())
    if not until_text:
        return now + timedelta(hours=2)  # default window

    # Durations like: '15 minutes', '20 min', '1 hour'
    m = re.match(r'^\s*(\d+)\s*(min|mins|minute|minutes|hr|hour|hours)?\s*$', until_text, re.I)
    if m:
        qty = int(m.group(1))
        unit = (m.group(2) or "minutes").lower()
        return now + (timedelta(hours=qty) if unit.startswith("hr") else timedelta(minutes=qty))

    # Times like '10:30 pm'
    t = dtparse(until_text, default=now.replace(hour=0, minute=0, second=0, microsecond=0))
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

# -----------------------------------------------------------------------------
# Track search
# -----------------------------------------------------------------------------

def search_track(sp: spotipy.Spotify, query: str) -> Optional[str]:
    """Return track URI for 'Title' or 'Title - Artist'."""
    title, artist = [s.strip() for s in (query.split(" - ") + [""])[:2]]
    q = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
    res = sp.search(q=q, type="track", limit=1)
    items = res.get("tracks", {}).get("items", [])
    return items[0]["uri"] if items else None

def search_tracks(sp: spotipy.Spotify, titles: List[str]) -> List[str]:
    uris: List[str] = []
    for t in titles:
        uri = search_track(sp, t)
        if not uri:
            raise ValueError(f'Could not find: {t}. Try "Title - Artist".')
        uris.append(uri)
    return uris

# -----------------------------------------------------------------------------
# Playlist building (fresh per session) and readiness wait
# -----------------------------------------------------------------------------

def create_session_playlist(sp: spotipy.Spotify, name_prefix="Loop Agent") -> str:
    me = sp.current_user()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = sp.user_playlist_create(
        me["id"],
        f"{name_prefix} ({ts})",
        public=False,
        description="Auto-loop session playlist",
    )
    return p["id"]

def fill_playlist_and_wait(sp: spotipy.Spotify, pid: str, uris: List[str], repeats: int = 200) -> None:
    """
    Fill the playlist with 'uris' repeated many times (emulates looping even
    when repeat toggling is restricted on the device) and wait until Spotify
    reports the full length before starting playback.
    """
    # Fill in batches of 100
    total_expected = len(uris) * repeats
    batch: List[str] = []
    for _ in range(repeats):
        batch.extend(uris)
        while len(batch) >= 100:
            sp.playlist_add_items(pid, batch[:100])
            batch = batch[100:]
    if batch:
        sp.playlist_add_items(pid, batch)

    # Wait for the reported count (avoid racing with stale snapshots)
    deadline = time.time() + 15  # up to ~15s
    while time.time() < deadline:
        pl = sp.playlist(pid, fields="tracks.total")
        if pl and pl.get("tracks", {}).get("total", 0) >= total_expected:
            break
        time.sleep(0.25)

# -----------------------------------------------------------------------------
# Device selection / activation
# -----------------------------------------------------------------------------

def find_device(sp: spotipy.Spotify, preferred_name: Optional[str]) -> Tuple[Optional[str], list]:
    preferred_name = preferred_name or os.getenv("DEFAULT_DEVICE_NAME")
    devices = sp.devices().get("devices", [])
    if not devices:
        return None, devices

    if preferred_name:
        # Exact match
        for d in devices:
            if d["name"].strip().lower() == preferred_name.strip().lower():
                return d["id"], devices
        # Substring fallback
        for d in devices:
            if preferred_name.strip().lower() in d["name"].strip().lower():
                return d["id"], devices

    # Prefer active device
    for d in devices:
        if d.get("is_active"):
            return d["id"], devices

    # Otherwise first
    return devices[0]["id"], devices

def ensure_device_active(sp: spotipy.Spotify, device_id: str, timeout_s: float = 8.0) -> bool:
    """Poll visibility and nudge transfer once to wake the device."""
    start = time.time()
    seen_once = False
    while time.time() - start < timeout_s:
        ds = sp.devices().get("devices", [])
        if any(d["id"] == device_id for d in ds):
            seen_once = True
            try:
                sp.transfer_playback(device_id=device_id, force_play=False)
            except Exception:
                pass
            return True
        time.sleep(0.4 if seen_once else 0.6)
    return False

# -----------------------------------------------------------------------------
# Robust start for iOS (break stale context, verify switch, fix mid-track)
# -----------------------------------------------------------------------------

def hard_start(sp: spotipy.Spotify, device_id: str, pl_uri: str) -> bool:
    """
    Steps:
      1) pause (break stale resume)
      2) transfer to device
      3) start playback once
      4) verify context matches our playlist
      5) if progress > 2s on first track, seek to 0 once (iOS quirk)
    """
    try:
        sp.pause_playback(device_id=device_id)
    except Exception:
        pass

    try:
        sp.transfer_playback(device_id=device_id, force_play=True)
    except Exception:
        pass
    time.sleep(0.7)

    sp.start_playback(device_id=device_id, context_uri=pl_uri, offset={"position": 0})
    time.sleep(1.0)

    try:
        pb = sp.current_playback()
        ctx = (pb or {}).get("context") or {}
        ok = (ctx.get("uri") == pl_uri)

        item = (pb or {}).get("item") or {}
        prog = (pb or {}).get("progress_ms") or 0
        if ok and prog > 2000 and item:
            try:
                sp.seek_track(0, device_id=device_id)
            except Exception:
                pass

        return ok
    except Exception:
        return False

def start_loop_and_schedule_stop(sp: spotipy.Spotify, uris: List[str], end_at: datetime, device_id: str) -> None:
    """Create a fresh playlist, wait until ready, start it reliably, and schedule stop."""
    pid = create_session_playlist(sp)
    fill_playlist_and_wait(sp, pid, uris, repeats=200)
    pl_uri = f"spotify:playlist:{pid}"

    if not ensure_device_active(sp, device_id):
        raise RuntimeError("Target device not active/visible on Spotify Connect.")

    ok = hard_start(sp, device_id, pl_uri)
    if not ok:
        time.sleep(0.8)
        ok = hard_start(sp, device_id, pl_uri)
    if not ok:
        raise RuntimeError("Could not switch playback to the session playlist.")

    # Best-effort: configure shuffle/repeat if allowed
    try:
        pb = sp.current_playback()
        dis = (pb or {}).get("actions", {}).get("disallows", {})
        if not dis.get("toggling_shuffle"):
            try:
                sp.shuffle(False, device_id=device_id)
            except Exception:
                pass
        if not dis.get("toggling_repeat_context"):
            try:
                sp.repeat("context", device_id=device_id)
            except Exception:
                pass
    except Exception:
        pass

    # Auto-stop
    def stopper():
        secs = (end_at - datetime.now(tz.tzlocal())).total_seconds()
        if secs > 0:
            time.sleep(secs)
        try:
            sp.pause_playback(device_id=device_id)
        except Exception:
            pass

    threading.Thread(target=stopper, daemon=True).start()

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/ping")
def ping():
    return "ok\n--\nTrue\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.get("/devices")
def devices():
    try:
        sp = sp_client()
        return jsonify(sp.devices())
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/status")
def status():
    try:
        sp = sp_client()
        return jsonify(playback=sp.current_playback())
    except Exception as e:
        return jsonify(error=str(e)), 500

# --- OAuth for cloud/server use ---
@app.get("/login")
def login():
    """Begin OAuth on a server. Visit /login once; then /play works via cached token."""
    try:
        auth = oauth_manager()
        return redirect(auth.get_authorize_url())
    except Exception as e:
        return f"Auth init error: {e}", 500

@app.get("/callback")
def callback():
    """Spotify redirect target after /login. Stores tokens in .cache-loop-agent."""
    try:
        code = request.args.get("code")
        if not code:
            return "Missing ?code param", 400
        auth = oauth_manager()
        auth.get_access_token(code)  # writes to cache_path
        return "Spotify login complete. You can close this tab.", 200
    except Exception as e:
        return f"Auth error: {e}", 500

# --- Main control ---
@app.post("/play")
def play():
    data = request.get_json(force=True)
    command = data.get("command", "")
    if not command:
        return jsonify(error="Missing 'command'"), 400

    try:
        titles, until_txt, dev_name = parse_cmd(command)
        end_at = resolve_until_today(until_txt)

        sp = sp_client()
        uris = search_tracks(sp, titles)

        device_id, _ = find_device(sp, dev_name)
        if not device_id:
            return jsonify(error="No active Spotify device found. Open Spotify on your phone/PC."), 409

        start_loop_and_schedule_stop(sp, uris, end_at, device_id)
        return jsonify(status="playing", count=len(uris), stop_at=end_at.isoformat())
    except Exception as e:
        return jsonify(error=str(e)), 500

# -----------------------------------------------------------------------------
# Local dev entrypoint (Render uses gunicorn server:app)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5055"))
    app.run(host="0.0.0.0", port=port)
