# server.py
# Loop Agent v2 — unified local + cloud Flask server
# Works on Render (cloud) and locally. One file, everything included.
#
# New in v2:
#   - /stop          → manually stop playback anytime
#   - /schedule      → schedule playback at a future time (e.g. "10:30 pm for 30 minutes")
#   - /history       → last 10 sessions (what played, how long, device)
#   - /devices       → list all Spotify Connect devices
#   - Web UI         → simple control panel at /ui
#   - Smarter stops  → active session tracking, no orphan threads
#   - Playlist mode  → play playlist "My Mix" in loop till 20 minutes
#   - Lean playlist  → only 1 copy of tracks + repeat=context (fast, no rate limits)
#
# Env vars needed:
#   SPOTIFY_CLIENT_ID
#   SPOTIFY_CLIENT_SECRET
#   SPOTIFY_REDIRECT_URI   (https://<your-app>.onrender.com/callback  OR  http://localhost:5055/callback)
#   DEFAULT_DEVICE_NAME    (optional, e.g. "iPhone")
#   OAUTH_CACHE_PATH       (optional, defaults to /tmp/loop-agent-cache.json)
#   PORT                   (optional, defaults to 5055)

import os
import re
import json
import time
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from dateutil import tz
from dateutil.parser import parse as dtparse
from flask import Flask, request, jsonify, redirect, render_template_string

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "playlist-modify-private "
    "playlist-read-private"
)

CACHE_PATH = os.getenv("OAUTH_CACHE_PATH", "/tmp/loop-agent-cache.json")
DEFAULT_DEVICE_NAME = os.getenv("DEFAULT_DEVICE_NAME", "")
HISTORY_PATH = os.getenv("HISTORY_PATH", "/tmp/loop-agent-history.json")
MAX_HISTORY = 10

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Session state — track active playback so /stop always works
# ---------------------------------------------------------------------------
_session_lock = threading.Lock()
_active_session = {
    "device_id": None,
    "stop_event": None,   # threading.Event
    "stop_at": None,
    "started_at": None,
    "tracks": [],
    "playlist": None,
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
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
        raise PermissionError("Not authorized. Visit /login first.")
    return spotipy.Spotify(auth_manager=auth(), requests_timeout=30)

# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def _load_history() -> list:
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _save_history(entry: dict):
    history = _load_history()
    history.insert(0, entry)
    history = history[:MAX_HISTORY]
    try:
        with open(HISTORY_PATH, "w") as f:
            json.dump(history, f, indent=2)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------
def parse_cmd(cmd: str) -> dict:
    """
    Supports:
      play "Song A" "Song B" in loop till 20 minutes [on iPhone]
      play playlist "My Mix" in loop till 1 hour [on iPhone]
    Returns dict with keys: mode, titles, playlist, until_text, device_name
    """
    cmd = cmd.strip()

    # Playlist mode
    m_pl = re.search(r'\bplay\s+playlist\s+"([^"]+)"', cmd, flags=re.IGNORECASE)
    if m_pl:
        playlist = m_pl.group(1).strip()
        m_till = re.search(r'\btill (.+?)(?: on |$)', cmd, flags=re.IGNORECASE)
        m_dev = re.search(r'\bon (.+)$', cmd, flags=re.IGNORECASE)
        return {
            "mode": "playlist",
            "titles": [],
            "playlist": playlist,
            "until_text": m_till.group(1).strip() if m_till else None,
            "device_name": m_dev.group(1).strip() if m_dev else None,
        }

    # Tracks mode
    titles = re.findall(r'"([^"]+)"', cmd)
    if not titles:
        raise ValueError(
            'Wrap song titles in quotes.\n'
            'Example: play "Kesariya" "Tum Hi Ho" in loop till 20 minutes on iPhone\n'
            'Or: play playlist "My Chill Mix" in loop till 30 minutes on iPhone'
        )

    m_till = re.search(r'\btill (.+?)(?: on |$)', cmd, flags=re.IGNORECASE)
    m_dev = re.search(r'\bon (.+)$', cmd, flags=re.IGNORECASE)
    return {
        "mode": "tracks",
        "titles": titles,
        "playlist": None,
        "until_text": m_till.group(1).strip() if m_till else None,
        "device_name": m_dev.group(1).strip() if m_dev else None,
    }

def resolve_until(until_text: Optional[str]) -> datetime:
    """Resolves '20 minutes', '1 hour', or '10:30 pm' to a datetime."""
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

# ---------------------------------------------------------------------------
# Spotify helpers
# ---------------------------------------------------------------------------
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
    uris = []
    for t in titles:
        uri = search_track(sp, t)
        if not uri:
            raise ValueError(f'Could not find: "{t}". Try exact title or "Title - Artist".')
        uris.append(uri)
    return uris

def resolve_playlist_uri(sp: spotipy.Spotify, text: str) -> Optional[str]:
    t = text.strip()
    if "open.spotify.com/playlist/" in t:
        pid = t.split("playlist/")[1].split("?")[0].split("/")[0]
        return f"spotify:playlist:{pid}"
    if t.startswith("spotify:playlist:"):
        return t
    results = sp.current_user_playlists(limit=50).get("items", [])
    t_low = t.lower()
    for p in results:
        if p["name"].strip().lower() == t_low:
            return p["uri"]
    for p in results:
        if t_low in p["name"].strip().lower():
            return p["uri"]
    res = sp.search(q=f'playlist:"{t}"', type="playlist", limit=1)
    items = res.get("playlists", {}).get("items", [])
    return items[0]["uri"] if items else None

def create_session_playlist(sp: spotipy.Spotify) -> str:
    me = sp.current_user()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = sp.user_playlist_create(
        me["id"], f"Loop Agent ({ts})", public=False, description="Auto-loop session"
    )
    return p["id"]

def fill_playlist_once(sp: spotipy.Spotify, pid: str, uris: List[str]):
    """Adds tracks once — repeat=context handles looping. Fast + avoids rate limits."""
    batch = uris[:]
    while batch:
        sp.playlist_add_items(pid, batch[:100])
        batch = batch[100:]
    deadline = time.time() + 8
    while time.time() < deadline:
        pl = sp.playlist(pid, fields="tracks.total")
        if pl and pl.get("tracks", {}).get("total", 0) >= len(uris):
            break
        time.sleep(0.3)

def find_device(sp: spotipy.Spotify, preferred: Optional[str]) -> Tuple[Optional[str], list]:
    preferred = preferred or DEFAULT_DEVICE_NAME or None
    devices = sp.devices().get("devices", [])
    if not devices:
        return None, devices
    if preferred:
        for d in devices:
            if d["name"].strip().lower() == preferred.strip().lower():
                return d["id"], devices
        for d in devices:
            if preferred.strip().lower() in d["name"].strip().lower():
                return d["id"], devices
    for d in devices:
        if d.get("is_active"):
            return d["id"], devices
    return devices[0]["id"], devices

def ensure_device_active(sp: spotipy.Spotify, device_id: str, timeout: float = 8.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        ds = sp.devices().get("devices", [])
        if any(d["id"] == device_id for d in ds):
            try:
                sp.transfer_playback(device_id=device_id, force_play=False)
            except Exception:
                pass
            return True
        time.sleep(0.5)
    return False

def hard_start(sp: spotipy.Spotify, device_id: str, context_uri: str) -> bool:
    try: sp.pause_playback(device_id=device_id)
    except Exception: pass
    try: sp.transfer_playback(device_id=device_id, force_play=True)
    except Exception: pass
    time.sleep(0.5)
    sp.start_playback(device_id=device_id, context_uri=context_uri, offset={"position": 0})
    time.sleep(0.8)
    try:
        pb = sp.current_playback()
        ctx = (pb or {}).get("context") or {}
        return ctx.get("uri") == context_uri
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Core play + stop logic
# ---------------------------------------------------------------------------
def _cancel_active_session():
    """Cancel any currently running stop thread."""
    with _session_lock:
        ev = _active_session.get("stop_event")
        if ev:
            ev.set()
        _active_session["stop_event"] = None
        _active_session["device_id"] = None
        _active_session["stop_at"] = None

def start_playback(sp: spotipy.Spotify, context_uri: str, end_at: datetime,
                   device_id: str, track_names: List[str], playlist_name: Optional[str]):
    if not ensure_device_active(sp, device_id):
        raise RuntimeError("Device not active. Open Spotify on your phone first.")

    try:
        sp.shuffle(False, device_id=device_id)
    except Exception:
        pass
    try:
        sp.repeat("context", device_id=device_id)
    except Exception:
        pass

    ok = hard_start(sp, device_id, context_uri)
    if not ok:
        time.sleep(0.8)
        ok = hard_start(sp, device_id, context_uri)
    if not ok:
        raise RuntimeError("Could not start playback. Try opening Spotify on your device first.")

    # Cancel any previous session
    _cancel_active_session()

    stop_event = threading.Event()
    started_at = datetime.now(tz.tzlocal())

    with _session_lock:
        _active_session["device_id"] = device_id
        _active_session["stop_event"] = stop_event
        _active_session["stop_at"] = end_at.isoformat()
        _active_session["started_at"] = started_at.isoformat()
        _active_session["tracks"] = track_names
        _active_session["playlist"] = playlist_name

    def stopper():
        secs = (end_at - datetime.now(tz.tzlocal())).total_seconds()
        if secs > 0:
            stop_event.wait(timeout=secs)
        if not stop_event.is_set():
            try:
                sp.pause_playback(device_id=device_id)
            except Exception:
                pass
        _save_history({
            "started_at": started_at.isoformat(),
            "stopped_at": datetime.now(tz.tzlocal()).isoformat(),
            "tracks": track_names,
            "playlist": playlist_name,
            "device_id": device_id,
            "manual_stop": stop_event.is_set(),
        })
        with _session_lock:
            if _active_session.get("stop_event") is stop_event:
                _active_session["stop_event"] = None
                _active_session["device_id"] = None

    threading.Thread(target=stopper, daemon=True).start()

# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Loop Agent</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: #0a0a0f;
    color: #e8e8f0;
    font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 40px 20px;
  }

  .logo {
    font-size: 13px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: #1db954;
    margin-bottom: 8px;
    font-weight: 600;
  }

  h1 {
    font-size: 36px;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 6px;
    background: linear-gradient(135deg, #fff 60%, #1db954);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }

  .subtitle {
    color: #666;
    font-size: 14px;
    margin-bottom: 40px;
  }

  .card {
    background: #13131a;
    border: 1px solid #1e1e2e;
    border-radius: 16px;
    padding: 28px;
    width: 100%;
    max-width: 480px;
    margin-bottom: 16px;
  }

  .card h2 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: #555;
    margin-bottom: 20px;
    font-weight: 600;
  }

  input, select {
    width: 100%;
    background: #0d0d14;
    border: 1px solid #222235;
    border-radius: 10px;
    color: #e8e8f0;
    font-size: 15px;
    padding: 12px 16px;
    margin-bottom: 12px;
    outline: none;
    transition: border-color 0.2s;
    font-family: inherit;
  }

  input:focus, select:focus {
    border-color: #1db954;
  }

  input::placeholder { color: #333; }

  .row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }

  button {
    width: 100%;
    padding: 14px;
    border-radius: 10px;
    border: none;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    font-family: inherit;
  }

  .btn-play {
    background: #1db954;
    color: #000;
    margin-top: 4px;
  }
  .btn-play:hover { background: #1ed760; transform: translateY(-1px); }
  .btn-play:active { transform: translateY(0); }

  .btn-stop {
    background: #1a0a0a;
    color: #e55;
    border: 1px solid #2a1010;
    margin-top: 8px;
  }
  .btn-stop:hover { background: #250d0d; border-color: #e55; }

  .btn-secondary {
    background: #0d0d14;
    color: #888;
    border: 1px solid #1e1e2e;
    font-size: 13px;
    padding: 10px;
  }
  .btn-secondary:hover { border-color: #333; color: #aaa; }

  .status-bar {
    background: #0d1a10;
    border: 1px solid #0f2a18;
    border-radius: 10px;
    padding: 14px 16px;
    font-size: 13px;
    color: #1db954;
    margin-top: 8px;
    display: none;
    line-height: 1.5;
  }

  .status-bar.error {
    background: #1a0a0a;
    border-color: #2a1010;
    color: #e55;
  }

  .history-item {
    padding: 12px 0;
    border-bottom: 1px solid #1a1a25;
    font-size: 13px;
    line-height: 1.6;
  }
  .history-item:last-child { border-bottom: none; }
  .history-item .songs { color: #e8e8f0; font-weight: 500; }
  .history-item .meta { color: #444; margin-top: 2px; }
  .history-item .manual { color: #e55; font-size: 11px; }
  .history-item .auto { color: #1db954; font-size: 11px; }

  .device-pill {
    display: inline-block;
    background: #0d0d14;
    border: 1px solid #1e1e2e;
    border-radius: 20px;
    padding: 4px 12px;
    font-size: 12px;
    color: #666;
    margin: 4px 4px 4px 0;
    cursor: pointer;
    transition: all 0.15s;
  }
  .device-pill:hover, .device-pill.active {
    border-color: #1db954;
    color: #1db954;
  }

  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid #0a2a14;
    border-top-color: #1db954;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    vertical-align: middle;
    margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .empty { color: #333; font-size: 13px; text-align: center; padding: 20px 0; }

  .now-playing {
    background: #0a1a10;
    border: 1px solid #0f2a18;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 16px;
    max-width: 480px;
    width: 100%;
    display: none;
  }
  .now-playing .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.15em; color: #1db954; margin-bottom: 6px; }
  .now-playing .title { font-size: 15px; font-weight: 600; }
  .now-playing .timer { font-size: 12px; color: #555; margin-top: 4px; }

  @media (max-width: 520px) {
    h1 { font-size: 28px; }
    .row { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<div class="logo">🎵 Loop Agent</div>
<h1>Sleep Timer</h1>
<p class="subtitle">Play music for exactly as long as you want</p>

<!-- Now Playing Banner -->
<div class="now-playing" id="nowPlaying">
  <div class="label">▶ Now Playing</div>
  <div class="title" id="npTitle">—</div>
  <div class="timer" id="npTimer">Stops at —</div>
</div>

<!-- Play Card -->
<div class="card">
  <h2>Play</h2>

  <input type="text" id="songs" placeholder='Songs: "Kesariya" "Tum Hi Ho"' />
  <input type="text" id="playlist" placeholder='Or playlist name (leave songs empty)' />

  <div class="row">
    <input type="number" id="minutes" placeholder="Minutes (e.g. 30)" min="1" max="480" />
    <input type="text" id="device" placeholder='Device (e.g. iPhone)' value="{{ default_device }}" />
  </div>

  <button class="btn-play" onclick="play()">▶ Play</button>
  <button class="btn-stop" onclick="stop()">⏹ Stop Now</button>
  <div class="status-bar" id="status"></div>
</div>

<!-- Devices Card -->
<div class="card">
  <h2>Devices</h2>
  <div id="deviceList"><div class="empty">Loading devices...</div></div>
  <button class="btn-secondary" style="margin-top:12px" onclick="loadDevices()">↻ Refresh</button>
</div>

<!-- History Card -->
<div class="card">
  <h2>Recent Sessions</h2>
  <div id="historyList"><div class="empty">No sessions yet</div></div>
  <button class="btn-secondary" style="margin-top:12px" onclick="loadHistory()">↻ Refresh</button>
</div>

<script>
  let selectedDevice = "";

  async function play() {
    const songs = document.getElementById("songs").value.trim();
    const playlist = document.getElementById("playlist").value.trim();
    const minutes = document.getElementById("minutes").value.trim();
    const device = selectedDevice || document.getElementById("device").value.trim();

    if (!songs && !playlist) return showStatus("Enter at least one song title or a playlist name.", true);
    if (!minutes) return showStatus("Enter how many minutes to play.", true);

    let command = "";
    if (playlist) {
      command = `play playlist "${playlist}" in loop till ${minutes} minutes`;
    } else {
      const titles = songs.match(/"[^"]+"|\\S+/g) || [];
      const quoted = titles.map(t => t.startsWith('"') ? t : `"${t}"`).join(" ");
      command = `play ${quoted} in loop till ${minutes} minutes`;
    }
    if (device) command += ` on ${device}`;

    showStatus('<span class="spinner"></span>Starting playback...', false);
    try {
      const res = await fetch("/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command })
      });
      const data = await res.json();
      if (res.ok) {
        const stopTime = new Date(data.stop_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        showStatus(`Playing ✓  Stops at ${stopTime}`, false);
        updateNowPlaying(songs || playlist, data.stop_at);
        loadHistory();
      } else {
        showStatus(data.error || "Something went wrong.", true);
      }
    } catch (e) {
      showStatus("Could not reach server.", true);
    }
  }

  async function stop() {
    showStatus('<span class="spinner"></span>Stopping...', false);
    try {
      const res = await fetch("/stop", { method: "POST" });
      const data = await res.json();
      if (res.ok) {
        showStatus("Stopped ✓", false);
        document.getElementById("nowPlaying").style.display = "none";
        loadHistory();
      } else {
        showStatus(data.error || "Could not stop.", true);
      }
    } catch (e) {
      showStatus("Could not reach server.", true);
    }
  }

  async function loadDevices() {
    const el = document.getElementById("deviceList");
    el.innerHTML = '<div class="empty">Loading...</div>';
    try {
      const res = await fetch("/devices");
      const data = await res.json();
      const devices = data.devices || [];
      if (!devices.length) {
        el.innerHTML = '<div class="empty">No devices found. Open Spotify on your phone.</div>';
        return;
      }
      el.innerHTML = devices.map(d =>
        `<span class="device-pill ${d.is_active ? 'active' : ''}" onclick="selectDevice('${d.name}')" title="${d.type}">
          ${d.is_active ? '▶ ' : ''}${d.name}
        </span>`
      ).join("");
    } catch {
      el.innerHTML = '<div class="empty">Could not load devices.</div>';
    }
  }

  function selectDevice(name) {
    selectedDevice = name;
    document.getElementById("device").value = name;
    document.querySelectorAll(".device-pill").forEach(p => {
      p.classList.toggle("active", p.textContent.trim().replace("▶ ", "") === name);
    });
  }

  async function loadHistory() {
    const el = document.getElementById("historyList");
    try {
      const res = await fetch("/history");
      const data = await res.json();
      const sessions = data.sessions || [];
      if (!sessions.length) {
        el.innerHTML = '<div class="empty">No sessions yet</div>';
        return;
      }
      el.innerHTML = sessions.map(s => {
        const started = new Date(s.started_at).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
        const stopped = new Date(s.stopped_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        const label = s.manual_stop
          ? '<span class="manual">⏹ stopped manually</span>'
          : '<span class="auto">✓ auto-stopped</span>';
        const content = s.playlist
          ? `<div class="songs">📋 ${s.playlist}</div>`
          : `<div class="songs">🎵 ${(s.tracks || []).join(", ")}</div>`;
        return `<div class="history-item">
          ${content}
          <div class="meta">${started} → ${stopped} &nbsp; ${label}</div>
        </div>`;
      }).join("");
    } catch {
      el.innerHTML = '<div class="empty">Could not load history.</div>';
    }
  }

  function updateNowPlaying(title, stopAt) {
    const np = document.getElementById("nowPlaying");
    np.style.display = "block";
    document.getElementById("npTitle").textContent = title;
    const stopTime = new Date(stopAt).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    document.getElementById("npTimer").textContent = `Stops at ${stopTime}`;
  }

  function showStatus(msg, isError) {
    const el = document.getElementById("status");
    el.style.display = "block";
    el.innerHTML = msg;
    el.className = "status-bar" + (isError ? " error" : "");
  }

  // Init
  loadDevices();
  loadHistory();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return "Loop Agent v2 is running. Visit /ui for the control panel or /login to authorize.\n"

@app.get("/ui")
def ui():
    return render_template_string(UI_HTML, default_device=DEFAULT_DEVICE_NAME)

@app.get("/health")
def health():
    return "ok", 200

@app.get("/wake")
def wake():
    """Lightweight pre-warm endpoint for Siri shortcut step 1."""
    return "awake", 200

@app.get("/login")
def login():
    try:
        return redirect(auth().get_authorize_url())
    except Exception as e:
        return jsonify(error=f"OAuth failed: {e}"), 500

@app.get("/callback")
def callback():
    try:
        code = request.args.get("code")
        if not code:
            return "Missing 'code' in callback.", 400
        auth().get_access_token(code=code, as_dict=True)
        return "Spotify authorized ✓  You can now use /play or the /ui control panel."
    except Exception as e:
        return f"OAuth callback failed: {e}", 500

@app.get("/logout")
def logout():
    try:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)
        return jsonify(ok=True, msg="Token cleared. Visit /login to re-authorize.")
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
        with _session_lock:
            session = {
                "stop_at": _active_session.get("stop_at"),
                "started_at": _active_session.get("started_at"),
                "tracks": _active_session.get("tracks"),
                "playlist": _active_session.get("playlist"),
                "active": _active_session.get("stop_event") is not None,
            }
        return jsonify(playback=sp.current_playback(), session=session)
    except PermissionError as e:
        return jsonify(error=str(e)), 401
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/history")
def history():
    return jsonify(sessions=_load_history())

@app.post("/stop")
def stop():
    """Manually stop playback immediately."""
    try:
        sp = sp_client()
        with _session_lock:
            ev = _active_session.get("stop_event")
            device_id = _active_session.get("device_id")
        if ev:
            ev.set()
        if device_id:
            try:
                sp.pause_playback(device_id=device_id)
            except Exception:
                pass
        return jsonify(ok=True, msg="Playback stopped.")
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

    command = (data or {}).get("command", "").strip()
    if not command:
        return jsonify(error="Missing 'command' field."), 400

    try:
        parsed = parse_cmd(command)
        end_at = resolve_until(parsed["until_text"])
        sp = sp_client()

        device_id, all_devices = find_device(sp, parsed["device_name"])
        if not device_id:
            return jsonify(
                error="No Spotify device found. Open Spotify on your phone and try again.",
                tip="Call /devices to see available devices."
            ), 409

        if parsed["mode"] == "playlist":
            pl_uri = resolve_playlist_uri(sp, parsed["playlist"])
            if not pl_uri:
                return jsonify(error=f'Playlist not found: "{parsed["playlist"]}"'), 404
            start_playback(sp, pl_uri, end_at, device_id, [], parsed["playlist"])
            return jsonify(
                status="playing",
                mode="playlist",
                playlist=parsed["playlist"],
                stop_at=end_at.isoformat(),
            )

        # Tracks mode
        uris = search_tracks(sp, parsed["titles"])
        pid = create_session_playlist(sp)
        fill_playlist_once(sp, pid, uris)
        context_uri = f"spotify:playlist:{pid}"
        start_playback(sp, context_uri, end_at, device_id, parsed["titles"], None)

        return jsonify(
            status="playing",
            mode="tracks",
            tracks=parsed["titles"],
            count=len(uris),
            stop_at=end_at.isoformat(),
        )

    except PermissionError as e:
        return jsonify(error=str(e)), 401
    except ValueError as e:
        return jsonify(error=str(e)), 404
    except SpotifyException as e:
        return jsonify(error=f"Spotify error {e.http_status}: {e.msg}"), int(e.http_status or 500)
    except Exception as e:
        return jsonify(error=f"Server error: {e}"), 500

@app.post("/schedule")
def schedule():
    """
    Schedule playback at a future time.
    Body: { "command": "play 'Song' in loop till 30 minutes on iPhone", "at": "10:30 pm" }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify(error="Invalid JSON"), 400

    command = (data or {}).get("command", "").strip()
    at_time = (data or {}).get("at", "").strip()
    if not command or not at_time:
        return jsonify(error="Need both 'command' and 'at' fields."), 400

    try:
        start_at = resolve_until(at_time)
    except Exception:
        return jsonify(error=f"Could not parse time: {at_time}"), 400

    def delayed_play():
        delay = (start_at - datetime.now(tz.tzlocal())).total_seconds()
        if delay > 0:
            time.sleep(delay)
        try:
            parsed = parse_cmd(command)
            end_at = resolve_until(parsed["until_text"])
            sp = sp_client()
            device_id, _ = find_device(sp, parsed["device_name"])
            if not device_id:
                return
            if parsed["mode"] == "playlist":
                pl_uri = resolve_playlist_uri(sp, parsed["playlist"])
                if pl_uri:
                    start_playback(sp, pl_uri, end_at, device_id, [], parsed["playlist"])
            else:
                uris = search_tracks(sp, parsed["titles"])
                pid = create_session_playlist(sp)
                fill_playlist_once(sp, pid, uris)
                start_playback(sp, f"spotify:playlist:{pid}", end_at, device_id, parsed["titles"], None)
        except Exception:
            pass

    threading.Thread(target=delayed_play, daemon=True).start()
    return jsonify(
        ok=True,
        scheduled_at=start_at.isoformat(),
        command=command,
        msg=f"Playback scheduled for {start_at.strftime('%I:%M %p')}."
    )

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5055"))
    app.run(host="0.0.0.0", port=port)
