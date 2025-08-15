# server_render.py
# --------------------------------------------------------------------
# Spotify "Loop Agent" - cloud build for Render (Flask + Gunicorn)
# - OAuth login: /login -> Spotify consent -> /callback
# - REST: POST /play, GET /status, GET /devices, GET /ping
# --------------------------------------------------------------------

import os
import re
import time
import threading
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

from dateutil import tz
from dateutil.parser import parse as dtparse
from flask import Flask, request, jsonify, redirect, url_for

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "playlist-modify-private "
    "playlist-read-private"
)

# Render sets PORT; default for local dev is 5055
PORT = int(os.environ.get("PORT", "5055"))

# In cloud we must not try to pop a browser
OPEN_BROWSER = False

CACHE_PATH = ".cache-loop-agent"  # safe name; do not commit to git

app = Flask(__name__)

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def sp_client() -> spotipy.Spotify:
    """Create an authenticated Spotify client. On Render this uses
    server-side OAuth and the token cache file (or cache in memory)."""
    missing = [k for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=OPEN_BROWSER,
        requests_timeout=30,
    )

    # If we don't yet have a token, tell the user to login
    token_info = auth.get_cached_token()
    if not token_info:
        raise RuntimeError("Not authorized. Visit /login to authorize this service with Spotify.")

    return spotipy.Spotify(auth=token_info["access_token"])

def parse_cmd(cmd: str) -> Tuple[List[str], Optional[str], Optional[str]]:
    titles = re.findall(r'"([^"]+)"', cmd)
    if not titles:
        raise ValueError('Use quotes: play "Song A" "Song B" in loop till 10 minutes [on iPhone]')

    m_till = re.search(r'\btill (.+?)(?: on |$)', cmd, flags=re.IGNORECASE)
    until_txt = m_till.group(1).strip() if m_till else None

    m_dev = re.search(r'\bon (.+)$', cmd, flags=re.IGNORECASE)
    device_name = m_dev.group(1).strip() if m_dev else None
    return titles, until_txt, device_name

def resolve_until_today(until_text: Optional[str]) -> datetime:
    now = datetime.now(tz.tzlocal())
    if not until_text:
        return now + timedelta(hours=2)

    m = re.match(r'^\s*(\d+)\s*(min|mins|minute|minutes|hr|hour|hours)?\s*$', until_text, re.I)
    if m:
        qty = int(m.group(1))
        unit = (m.group(2) or "minutes").lower()
        return now + (timedelta(hours=qty) if unit.startswith("hr") else timedelta(minutes=qty))

    t = dtparse(until_text, default=now.replace(hour=0, minute=0, second=0, microsecond=0))
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

def search_track(sp: spotipy.Spotify, query: str) -> Optional[str]:
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
    total_expected = len(uris) * repeats
    batch: List[str] = []
    for _ in range(repeats):
        batch.extend(uris)
        while len(batch) >= 100:
            sp.playlist_add_items(pid, batch[:100])
            batch = batch[100:]
    if batch:
        sp.playlist_add_items(pid, batch)

    # wait until Spotify reports the full length to avoid snapshot race
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

        # nudge to 0 if iOS dropped us mid-track
        prog = (pb or {}).get("progress_ms") or 0
        if ok and prog > 2000:
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

    # best-effort repeat/shuffle
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

    def stopper():
        secs = (end_at - datetime.now(tz.tzlocal())).total_seconds()
        if secs > 0:
            time.sleep(secs)
        try:
            sp.pause_playback(device_id=device_id)
        except Exception:
            pass

    threading.Thread(target=stopper, daemon=True).start()

# --------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------

@app.get("/")
def root():
    return jsonify(ok=True, msg="Loop Agent up. Use /login to authorize or POST /play.")

@app.get("/ping")
def ping():
    return "ok\n--\nTrue\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.get("/login")
def login():
    """Start OAuth flow."""
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=OPEN_BROWSER,
    )
    return redirect(auth.get_authorize_url())

@app.get("/callback")
def callback():
    """Finish OAuth flow."""
    code = request.args.get("code")
    if not code:
        return "Missing code", 400
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=OPEN_BROWSER,
    )
    auth.get_access_token(code, as_dict=False)
    return "Authorized. You can now POST /play.", 200

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
            return jsonify(error="No active Spotify device found. Open Spotify on your device."), 409

        start_loop_and_schedule_stop(sp, uris, end_at, device_id)
        return jsonify(status="playing", count=len(uris), stop_at=end_at.isoformat())
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/status")
def status():
    try:
        sp = sp_client()
        return jsonify(playback=sp.current_playback())
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/devices")
def devices():
    try:
        sp = sp_client()
        return jsonify(sp.devices())
    except Exception as e:
        return jsonify(error=str(e)), 500

# For local dev only
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
