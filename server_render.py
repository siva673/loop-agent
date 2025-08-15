# server_render.py
# -----------------------------------------------------------------------------
# Spotify "Loop Agent" — cloud-friendly version for Render.com (and local use)
#
# What it does:
#   - /login    -> starts Spotify OAuth
#   - /callback -> completes OAuth, stores token cache in /tmp (safe on Render)
#   - /play     -> POST { "command": 'play "A" "B" in loop till 15 minutes on iPhone' }
#   - /status   -> current playback payload
#   - /devices  -> Spotify Connect devices
#   - /ping     -> plain-text health check
#   - /healthz  -> HTTP 200 for Render health checks
#
# Notes:
#   * Set env vars in Render -> Environment:
#       SPOTIFY_CLIENT_ID          (from Spotify dashboard)
#       SPOTIFY_CLIENT_SECRET      (from Spotify dashboard)
#       SPOTIFY_REDIRECT_URI       https://YOUR-SERVICE.onrender.com/callback
#       DEFAULT_DEVICE_NAME        (optional, e.g. iPhone)
#       OAUTH_CACHE_PATH           (optional; default = /tmp/.cache-loop-agent)
#   * In Spotify dashboard, add EXACT redirect URI above.
#   * Procfile: web: gunicorn server_render:app
# -----------------------------------------------------------------------------

import os
import re
import time
import threading
import traceback
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

from dateutil import tz
from dateutil.parser import parse as dtparse
from flask import Flask, request, jsonify, redirect

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)

# -----------------------------------------------------------------------------
# OAuth / Spotipy helpers
# -----------------------------------------------------------------------------
def _oauth_cache_path() -> str:
    # Default to a writeable location on Render; also fine locally.
    p = os.getenv("OAUTH_CACHE_PATH", "/tmp/.cache-loop-agent")
    d = os.path.dirname(p) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return p

SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "playlist-modify-private "
    "playlist-read-private"
)

def _oauth() -> SpotifyOAuth:
    """Configured OAuth manager. Cache goes to /tmp by default."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI")

    if not all([client_id, client_secret, redirect_uri]):
        raise RuntimeError(
            "Missing Spotify env vars. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI."
        )

    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        cache_path=_oauth_cache_path(),
        open_browser=False,          # Render should not try to open a local browser
        requests_timeout=30,
    )

def sp_client() -> spotipy.Spotify:
    """Create an authenticated Spotify client using cached token (or refresh)."""
    oauth = _oauth()
    token_info = oauth.get_cached_token()
    if not token_info:
        # No token yet—user must visit /login
        raise RuntimeError("Not authorized with Spotify yet. Visit /login first.")
    # refresh if needed
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
    return spotipy.Spotify(auth=token_info["access_token"], requests_timeout=30)

# -----------------------------------------------------------------------------
# Command parsing + time helpers
# -----------------------------------------------------------------------------
def parse_cmd(cmd: str) -> Tuple[List[str], Optional[str], Optional[str]]:
    """
    Flexible pattern:
      play "A" ["B" ...] in loop till <time-or-duration> [on <device>]
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
    """Turn '15 minutes' or '10:30 pm' into an absolute timestamp (local tz)."""
    now = datetime.now(tz.tzlocal())
    if not until_text:
        return now + timedelta(hours=2)

    # Duration like '15 minutes', '20 min', '1 hour'
    m = re.match(r'^\s*(\d+)\s*(min|mins|minute|minutes|hr|hour|hours)?\s*$', until_text, re.I)
    if m:
        qty = int(m.group(1))
        unit = (m.group(2) or "minutes").lower()
        return now + (timedelta(hours=qty) if unit.startswith("hr") else timedelta(minutes=qty))

    # Time of day like '10:30 pm'
    t = dtparse(until_text, default=now.replace(hour=0, minute=0, second=0, microsecond=0))
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

# -----------------------------------------------------------------------------
# Track / playlist helpers
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
    Fill the playlist with uris repeated 'repeats' times (to emulate looping
    even when repeat toggling is restricted), and wait until Spotify reports
    the expected length before starting playback.
    """
    total_expected = len(uris) * repeats
    batch: List[str] = []
    added = 0

    for _ in range(repeats):
        batch.extend(uris)
        while len(batch) >= 100:
            sp.playlist_add_items(pid, batch[:100])
            added += 100
            batch = batch[100:]
    if batch:
        sp.playlist_add_items(pid, batch)
        added += len(batch)

    # Wait until the playlist shows the full count
    deadline = time.time() + 15
    while time.time() < deadline:
        pl = sp.playlist(pid, fields="tracks.total")
        if pl and pl.get("tracks", {}).get("total", 0) >= total_expected:
            break
        time.sleep(0.25)

def find_device(sp: spotipy.Spotify, preferred_name: Optional[str]) -> Tuple[Optional[str], list]:
    preferred_name = preferred_name or os.getenv("DEFAULT_DEVICE_NAME")
    devices = sp.devices().get("devices", [])
    if not devices:
        return None, devices

    if preferred_name:
        for d in devices:
            if d["name"].strip().lower() == preferred_name.strip().lower():
                return d["id"], devices
        for d in devices:
            if preferred_name.strip().lower() in d["name"].strip().lower():
                return d["id"], devices

    for d in devices:
        if d.get("is_active"):
            return d["id"], devices
    return devices[0]["id"], devices

def ensure_device_active(sp: spotipy.Spotify, device_id: str, timeout_s: float = 8.0) -> bool:
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

def hard_start(sp: spotipy.Spotify, device_id: str, pl_uri: str) -> bool:
    """Deterministic start on iOS."""
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

        # If we landed mid-track (common on iOS if resuming), seek to 0 once.
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

    # Best-effort: set shuffle/repeat depending on device restrictions
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

    # Auto-stop at end_at
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
@app.get("/")
def root():
    return (
        "Loop Agent is running. Use /login to authorize, then call POST /play.\n",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
    )

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/ping")
def ping():
    return "ok\n--\nTrue\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.get("/login")
def login():
    """Kick off Spotify OAuth. Visit this in a browser once after deploy."""
    try:
        oauth = _oauth()
        auth_url = oauth.get_authorize_url()
        return redirect(auth_url)
    except Exception as e:
        app.logger.error("Login error: %s\n%s", e, traceback.format_exc())
        return "OAuth init failed. Check env vars and redirect URI.", 500

@app.get("/callback")
def callback():
    """Spotify redirects here after user signs in & approves scopes."""
    try:
        oauth = _oauth()
        code = oauth.parse_response_code(request.url)
        if not code:
            return "Missing 'code' in callback URL.", 400

        # spotipy 2.23 returns dict; handle both styles
        token_info = oauth.get_access_token(code)
        if isinstance(token_info, str):  # rare old style returns access_token str
            # force a cache write by constructing the client
            spotipy.Spotify(auth=token_info)
        # If dict, OAuth class already cached it at cache_path

        return (
            "Spotify authorization complete. "
            "You can now POST /play with your command JSON.",
            200,
        )
    except Exception as e:
        app.logger.error("Callback error: %s\n%s", e, traceback.format_exc())
        return "OAuth callback failed. See server logs.", 500

@app.post("/play")
def play():
    """POST /play  { "command": "play \"A\" \"B\" in loop till 15 minutes on iPhone" }"""
    try:
        data = request.get_json(force=True) or {}
        command = data.get("command", "")
        if not command:
            return jsonify(error="Missing 'command'"), 400

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
        app.logger.error("Play error: %s\nPayload: %s\n%s", e, request.data, traceback.format_exc())
        return jsonify(error=str(e)), 500

@app.get("/status")
def status():
    try:
        sp = sp_client()
        return jsonify(playback=sp.current_playback())
    except Exception as e:
        app.logger.error("Status error: %s\n%s", e, traceback.format_exc())
        return jsonify(error=str(e)), 500

@app.get("/devices")
def devices():
    try:
        sp = sp_client()
        return jsonify(sp.devices())
    except Exception as e:
        app.logger.error("Devices error: %s\n%s", e, traceback.format_exc())
        return jsonify(error=str(e)), 500

# -----------------------------------------------------------------------------
# Local dev entrypoint (Render uses Gunicorn with `server_render:app`)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Local: flask dev server; Render will NOT use this.
    port = int(os.getenv("PORT", "5055"))
    app.run(host="0.0.0.0", port=port, debug=True)
