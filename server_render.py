# server_render.py
# Loop Agent — cloud-friendly Flask service for Spotify control (fast version)

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
from spotipy.exceptions import SpotifyException

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "playlist-modify-private "
    "playlist-read-private"
)

CACHE_PATH = os.getenv("OAUTH_CACHE_PATH", "/tmp/loop-agent-cache.json")
DEFAULT_DEVICE_NAME = os.getenv("DEFAULT_DEVICE_NAME", "")

app = Flask(__name__)

# prevent overlapping /play operations
_play_lock = threading.Lock()

# -----------------------------------------------------------------------------
# Auth (lazy + thread-safe)
# -----------------------------------------------------------------------------
_auth_lock = threading.Lock()
_auth_obj: Optional[SpotifyOAuth] = None

def auth() -> SpotifyOAuth:
    global _auth_obj
    with _auth_lock:
        if _auth_obj is None:
            _auth_obj = SpotifyOAuth(
                client_id=os.environ["SPOTIFY_CLIENT_ID"],
                client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
                redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
                scope=SCOPES,
                cache_path=CACHE_PATH,
                open_browser=False,
                requests_timeout=30,
            )
        return _auth_obj

def sp_client(ensure_auth: bool = True) -> spotipy.Spotify:
    if ensure_auth and not auth().get_cached_token():
        raise PermissionError("Not authorized with Spotify. Visit /login first.")
    return spotipy.Spotify(auth_manager=auth(), requests_timeout=30)

# -----------------------------------------------------------------------------
# Command parsing & time handling
# -----------------------------------------------------------------------------
def parse_cmd(cmd: str) -> Tuple[List[str], Optional[str], Optional[str]]:
    titles = re.findall(r'"([^"]+)"', cmd)
    if not titles:
        raise ValueError('Use quotes: play "Song A" ["Song B"...] in loop till 10 minutes [on iPhone]')

    m_till = re.search(r'\btill (.+?)(?: on |$)', cmd, flags=re.I)
    until_txt = m_till.group(1).strip() if m_till else None

    m_dev = re.search(r'\bon (.+)$', cmd, flags=re.I)
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

# -----------------------------------------------------------------------------
# Spotify helpers
# -----------------------------------------------------------------------------
def search_track(sp: spotipy.Spotify, query: str) -> Optional[str]:
    title, artist = [s.strip() for s in (query.split(" - ") + [""])[:2]]
    primary = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
    for q in (primary, f'track:"{title}"'):
        res = sp.search(q=q, type="track", limit=1)
        items = res.get("tracks", {}).get("items", [])
        if items:
            return items[0]["uri"]
    return None

def search_tracks(sp: spotipy.Spotify, titles: List[str]) -> List[str]:
    uris: List[str] = []
    for t in titles:
        uri = search_track(sp, t)
        if not uri:
            raise ValueError(f'Could not find: {t}. Try using just the title or correct "Title - Artist".')
        uris.append(uri)
    return uris

def create_session_playlist(sp: spotipy.Spotify, name_prefix="Loop Agent") -> str:
    me = sp.current_user()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = sp.user_playlist_create(
        me["id"], f"{name_prefix} ({ts})", public=False, description="Auto-loop session"
    )
    return p["id"]

def fill_playlist_once(sp: spotipy.Spotify, pid: str, uris: List[str]) -> None:
    batch = uris[:]
    while batch:
        sp.playlist_add_items(pid, batch[:100])
        batch = batch[100:]
    # best-effort confirm
    deadline = time.time() + 6
    need = len(uris)
    while time.time() < deadline:
        pl = sp.playlist(pid, fields="tracks.total")
        if pl and pl.get("tracks", {}).get("total", 0) >= need:
            break
        time.sleep(0.2)

def find_device(sp: spotipy.Spotify, preferred_name: Optional[str]) -> Tuple[Optional[str], list]:
    preferred_name = preferred_name or DEFAULT_DEVICE_NAME or None
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
    time.sleep(0.5)

    sp.start_playback(device_id=device_id, context_uri=pl_uri, offset={"position": 0})
    time.sleep(0.7)

    try:
        pb = sp.current_playback()
        ctx = (pb or {}).get("context") or {}
        ctx_uri = ctx.get("uri")          # <- fixed line (no double dot)
        return ctx_uri == pl_uri
    except Exception:
        return False

def start_loop_and_schedule_stop(sp: spotipy.Spotify, uris: List[str], end_at: datetime, device_id: str) -> None:
    pid = create_session_playlist(sp)
    fill_playlist_once(sp, pid, uris)
    pl_uri = f"spotify:playlist:{pid}"

    if not ensure_device_active(sp, device_id):
        raise RuntimeError("Target device not active/visible on Spotify Connect.")

    try:
        try: sp.shuffle(False, device_id=device_id)
        except Exception: pass
        try: sp.repeat("context", device_id=device_id)
        except Exception: pass

        if not hard_start(sp, device_id, pl_uri):
            time.sleep(0.6)
            if not hard_start(sp, device_id, pl_uri):
                raise RuntimeError("Could not switch playback to the session playlist.")
    except SpotifyException as e:
        raise RuntimeError(f"Spotify error {e.http_status}: {e.msg}")

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
    return "Loop Agent is running. Visit /login to authorize, /health for ping.\n"

@app.get("/health")
def health():
    return "ok", 200

@app.get("/login")
def login():
    try:
        return redirect(auth().get_authorize_url())
    except Exception as e:
        return jsonify(error=f"Failed to start OAuth: {e}"), 500

@app.get("/callback")
def callback():
    try:
        code = request.args.get("code")
        if not code:
            return "OAuth callback missing 'code' parameter.", 400
        token_info = auth().get_access_token(code=code, as_dict=True)
        if not token_info:
            return "OAuth callback failed to obtain token.", 500
        return "Spotify authorization complete. You can now POST /play with your command JSON."
    except Exception as e:
        return f"OAuth callback failed: {e}", 500

@app.get("/logout")
def logout():
    try:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)
        return jsonify(ok=True, cache_removed=True, cache_path=CACHE_PATH)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/whoami")
def whoami():
    try:
        sp = sp_client()
        me = sp.current_user()
        return jsonify(id=me.get("id"), display_name=me.get("display_name"), product=me.get("product"))
    except PermissionError as e:
        return jsonify(error=str(e)), 401
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/devices")
def devices():
    try:
        sp = sp_client()
        return jsonify(sp.devices())
    except PermissionError as e:
        return jsonify(error=str(e)), 401
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/status")
def status():
    try:
        sp = sp_client()
        return jsonify(playback=sp.current_playback())
    except PermissionError as e:
        return jsonify(error=str(e)), 401
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/play")
def play():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify(error="Invalid JSON body"), 400

    command = (data or {}).get("command", "")
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

        with _play_lock:
            start_loop_and_schedule_stop(sp, uris, end_at, device_id)

        return jsonify(status="playing", count=len(uris), stop_at=end_at.isoformat())

    except PermissionError as e:
        return jsonify(error=str(e)), 401
    except ValueError as e:
        return jsonify(error=str(e)), 404
    except SpotifyException as e:
        return jsonify(error=f"Spotify error {e.http_status}: {e.msg}"), int(e.http_status or 500)
    except Exception as e:
        return jsonify(error=f"Server error: {e}"), 500
@app.get("/")
def root():
    return "ok", 200
 
@app.get("/health")
def health():
    return "ok", 200

# -----------------------------------------------------------------------------
# Local dev (Render uses gunicorn)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5055")))

