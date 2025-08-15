# server_render.py
# Loop Agent (Render edition) — OAuth fixed (state + secret), same endpoints.

import os
import re
import time
import threading
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

from flask import Flask, request, jsonify, redirect, session, url_for, make_response
from dateutil import tz
from dateutil.parser import parse as dtparse

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ---------- Config from env ----------
SPOTIFY_CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
SPOTIFY_REDIRECT_URI = os.environ["SPOTIFY_REDIRECT_URI"]  # e.g. https://loop-agent.onrender.com/callback
DEFAULT_DEVICE_NAME = os.environ.get("DEFAULT_DEVICE_NAME", "iPhone")
OAUTH_CACHE_PATH = os.environ.get("OAUTH_CACHE_PATH", "/tmp/.cache-loop-agent")

# Flask & session (REQUIRED for OAuth state)
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET", os.environ.get("SECRET_KEY", "change-me"))
app.config["SESSION_COOKIE_SECURE"] = True  # HTTPS on Render
app.config["PREFERRED_URL_SCHEME"] = "https"

SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "playlist-modify-private "
    "playlist-read-private"
)

def sp_client() -> spotipy.Spotify:
    """Authenticated Spotify client; requires prior /login & /callback to populate cache."""
    auth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SCOPES,
        cache_path=OAUTH_CACHE_PATH,
        open_browser=False,
        requests_timeout=30,
    )
    # Will refresh if needed using refresh_token in cache
    token_info = auth.get_cached_token()
    if not token_info:
        raise RuntimeError("Not authorized with Spotify. Visit /login first.")
    return spotipy.Spotify(auth=token_info["access_token"])

# ---------- Helpers ----------
def parse_cmd(cmd: str):
    titles = re.findall(r'"([^"]+)"', cmd)
    if not titles:
        raise ValueError('Use quotes: play "Song A" ["Song B"] till 10 minutes [on iPhone]')

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

def search_track(sp: spotipy.Spotify, query: str) -> Optional[str]:
    title, artist = [s.strip() for s in (query.split(" - ") + [""])[:2]]
    q = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
    res = sp.search(q=q, type="track", limit=1)
    items = res.get("tracks", {}).get("items", [])
    return items[0]["uri"] if items else None

def search_tracks(sp: spotipy.Spotify, titles: List[str]) -> List[str]:
    uris = []
    for t in titles:
        uri = search_track(sp, t)
        if not uri:
            raise ValueError(f'Could not find: {t}. Try "Title - Artist".')
        uris.append(uri)
    return uris

def create_session_playlist(sp: spotipy.Spotify, name_prefix="Loop Agent") -> str:
    me = sp.current_user()
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    p = sp.user_playlist_create(
        me["id"],
        f"{name_prefix} ({ts})",
        public=False,
        description="Auto-loop session playlist",
    )
    return p["id"]

def fill_playlist_and_wait(sp: spotipy.Spotify, pid: str, uris: List[str], repeats: int = 200) -> None:
    total_expected = len(uris) * repeats
    batch, added = [], 0
    for _ in range(repeats):
        batch.extend(uris)
        while len(batch) >= 100:
            sp.playlist_add_items(pid, batch[:100])
            added += 100
            batch = batch[100:]
    if batch:
        sp.playlist_add_items(pid, batch)
        added += len(batch)

    deadline = time.time() + 15
    while time.time() < deadline:
        pl = sp.playlist(pid, fields="tracks.total")
        if pl and pl.get("tracks", {}).get("total", 0) >= total_expected:
            break
        time.sleep(0.25)

def find_device(sp: spotipy.Spotify, preferred_name: Optional[str]):
    preferred_name = (preferred_name or DEFAULT_DEVICE_NAME or "").strip()
    devices = sp.devices().get("devices", [])
    if not devices:
        return None, devices

    if preferred_name:
        for d in devices:
            if d["name"].strip().lower() == preferred_name.lower():
                return d["id"], devices
        for d in devices:
            if preferred_name.lower() in d["name"].strip().lower():
                return d["id"], devices

    for d in devices:
        if d.get("is_active"):
            return d["id"], devices
    return devices[0]["id"], devices

def ensure_device_active(sp: spotipy.Spotify, device_id: str, timeout_s: float = 8.0) -> bool:
    start, seen_once = time.time(), False
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

# ---------- Routes ----------
@app.get("/")
def root():
    return "Loop Agent online. Use /login to connect Spotify.", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/ping")
def ping():
    return "ok\n--\nTrue\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.get("/login")
def login():
    # Start OAuth and store state in session
    auth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SCOPES,
        cache_path=OAUTH_CACHE_PATH,
        open_browser=False,
    )
    auth_url = auth.get_authorize_url()
    # spotipy embeds state in the URL; extract and save it for validation
    # (it’s the value after `state=` if present)
    m = re.search(r"[?&]state=([^&]+)", auth_url)
    if m:
        session["oauth_state"] = m.group(1)
    return redirect(auth_url, code=302)

@app.get("/callback")
def callback():
    error = request.args.get("error")
    code = request.args.get("code")
    state = request.args.get("state")

    if error:
        return f"Spotify error: {error}", 400

    expected_state = session.pop("oauth_state", None)
    if expected_state and state and state != expected_state:
        return "OAuth state mismatch.", 400

    auth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SCOPES,
        cache_path=OAUTH_CACHE_PATH,
        open_browser=False,
    )

    try:
        # Exchange code and write tokens to cache_path
        token_info = auth.get_access_token(code, check_cache=False)
        if not token_info:
            return "OAuth callback failed (no token).", 500
        # Success page
        resp = make_response("Authenticated with Spotify. You can close this tab.")
        return resp
    except Exception as e:
        return f"OAuth callback failed. See server logs. ({e})", 500

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

@app.post("/play")
def play():
    data = request.get_json(force=True, silent=True) or {}
    command = data.get("command", "")

    if not command:
        return jsonify(error="Missing 'command'"), 400

    try:
        sp = sp_client()

        titles, until_txt, dev_name = parse_cmd(command)
        end_at = resolve_until_today(until_txt)
        uris = search_tracks(sp, titles)

        device_id, _ = find_device(sp, dev_name)
        if not device_id:
            return jsonify(error="No active Spotify device found. Open Spotify on your phone/PC."), 409

        start_loop_and_schedule_stop(sp, uris, end_at, device_id)
        return jsonify(status="playing", count=len(uris), stop_at=end_at.isoformat())
    except Exception as e:
        return jsonify(error=str(e)), 500

# Render/Gunicorn entrypoint
if __name__ == "__main__":
    # For local testing only
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5055")))
