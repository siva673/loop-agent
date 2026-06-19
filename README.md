# Loop Agent v2 🎵

> Spotify sleep timer with voice control, web UI, and smart auto-stop.

Say: *"Hey Siri, Loop Agent — play Kesariya Tum Hi Ho for 20 minutes"* and sleep peacefully. Music stops automatically.

## What's new in v2

- **Single file** — one `server.py` works locally and on Render cloud
- **Web UI** at `/ui` — play, stop, device picker, session history
- **Manual stop** — `/stop` endpoint, also in the UI
- **Multi-device** — see all Spotify Connect devices, pick any
- **Scheduled playback** — POST `/schedule` with an `at` time
- **Session history** — last 10 sessions logged automatically
- **Smarter looping** — 1 copy of tracks + repeat=context (faster, fewer rate limit hits)
- **Playlist mode** — play any of your Spotify playlists by name or URL
- **/wake endpoint** — lightweight pre-warm for Siri (no full /play needed)

---

## Quick Setup

### 1. Spotify Developer App

Go to https://developer.spotify.com/dashboard and create an app.

Add these Redirect URIs:
- Local: `http://localhost:5055/callback`
- Render: `https://your-app-name.onrender.com/callback`

### 2. Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```
SPOTIFY_CLIENT_ID=your_id
SPOTIFY_CLIENT_SECRET=your_secret
SPOTIFY_REDIRECT_URI=http://localhost:5055/callback
DEFAULT_DEVICE_NAME=iPhone
```

### 3. Local Run

```bash
python -m venv .venv
source .venv/bin/activate      # Mac/Linux
# .venv\Scripts\Activate.ps1  # Windows

pip install -r requirements.txt
python server.py
```

Open http://localhost:5055/login once to authorize Spotify.

Then visit http://localhost:5055/ui for the control panel.

### 4. Render Cloud Deploy

1. Push this repo to GitHub
2. Create a Render Web Service (Python)
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn server:app --workers 2 --timeout 120 --log-level info`
5. Health Check Path: `/health`
6. Add env vars in Render dashboard
7. Open `https://your-app.onrender.com/login` once to authorize

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Status check |
| GET | `/ui` | Web control panel |
| GET | `/health` | Liveness ping |
| GET | `/wake` | Lightweight pre-warm (use in Siri step 1) |
| GET | `/login` | Start Spotify OAuth |
| GET | `/callback` | OAuth callback (set in Spotify Dashboard) |
| GET | `/logout` | Clear token cache |
| GET | `/whoami` | Current Spotify user |
| GET | `/devices` | List Spotify Connect devices |
| GET | `/status` | Playback + active session info |
| GET | `/history` | Last 10 sessions |
| POST | `/play` | Start playback |
| POST | `/stop` | Stop playback immediately |
| POST | `/schedule` | Schedule playback at a time |

---

## Play Command Format

```json
{ "command": "play \"Song A\" \"Song B\" in loop till 20 minutes on iPhone" }
```

With artist:
```json
{ "command": "play \"Kesariya - Arijit Singh\" in loop till 30 minutes on iPhone" }
```

Playlist:
```json
{ "command": "play playlist \"My Chill Mix\" in loop till 1 hour on iPhone" }
```

Clock time:
```json
{ "command": "play \"Tum Hi Ho\" in loop till 10:30 pm on iPhone" }
```

---

## Schedule Playback

```bash
curl -X POST https://your-app.onrender.com/schedule \
  -H "Content-Type: application/json" \
  -d '{"command": "play \"Levitating\" in loop till 30 minutes on iPhone", "at": "10:30 pm"}'
```

---

## Manual Stop

```bash
curl -X POST https://your-app.onrender.com/stop
```

Or just click Stop in the web UI.

---

## Siri Shortcut Setup

1. Open Shortcuts app → + → name it **Loop Agent**
2. Add **Get Contents of URL**
   - Method: GET
   - URL: `https://your-app.onrender.com/wake`
3. Add **Wait** — 2 seconds (gives Render free tier time to wake)
4. Add **Dictate Text**
   - Prompt: `Say your Loop Agent command`
5. Add **Get Contents of URL**
   - Method: POST
   - URL: `https://your-app.onrender.com/play`
   - Headers: `Content-Type: application/json`
   - Body (JSON): `{ "command": [Dictated Text] }`
6. Add **Show Result** (optional — shows server response)

Say: *"Hey Siri, Loop Agent"* then speak your command.

**Example voice command:**
> "play Blinding Lights Levitating in loop till 20 minutes on iPhone"

---

## Notes

- Spotify Premium recommended for full playback control
- Render free tier sleeps after 15min inactivity — first Siri call may be slow (~50s). The `/wake` pre-ping helps.
- Render paid ($7/mo) stays warm for instant response
- Keep Spotify open on your phone so the device shows up

---

## Built With

Python · Flask · Spotipy · Siri Shortcuts · Render · GitHub

MIT License — use freely with attribution.
