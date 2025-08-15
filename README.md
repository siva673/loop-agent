Loop Agent (Spotify) — Flask on Render

Control Spotify with one simple command, e.g.:

play "Butta Bomma" "Samajavaragamana" in loop till 20 minutes on iPhone


The server builds a tiny private playlist on the fly, starts playback on your device, toggles repeat=context, and auto-stops at the time you asked.

Render deploy:

Put server_render.py and requirements.txt in your repo.

Create a Spotify app at https://developer.spotify.com/dashboard and add this redirect URI:

https://<your-service>.onrender.com/callback

Create a Render → Web Service (Python):

Build Command: pip install -r requirements.txt

Start Command: gunicorn server_render:app --workers 2 --timeout 120 --log-level info

Health Check Path: /health

Env Vars:

SPOTIFY_CLIENT_ID = your id

SPOTIFY_CLIENT_SECRET = your secret

SPOTIFY_REDIRECT_URI = https://<your-service>.onrender.com/callback

(optional) DEFAULT_DEVICE_NAME = iPhone

Open https://<your-service>.onrender.com/login once to authorize Spotify.

POST /play with your command JSON (examples below). Done.

Local dev (optional)
git clone https://github.com/<you>/loop-agent.git
cd loop-agent

python -m venv .venv
# mac/linux:
source .venv/bin/activate
# windows powershell:
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# env vars (examples)
export SPOTIFY_CLIENT_ID=xxx
export SPOTIFY_CLIENT_SECRET=yyy
export SPOTIFY_REDIRECT_URI=http://localhost:5055/callback
export DEFAULT_DEVICE_NAME=iPhone

python server_render.py
# then open http://localhost:5055/login once to authorize

API

Health
GET /health → "ok"

Start OAuth
GET /login → redirects to Spotify

Spotify callback
GET /callback → set this exact URI in Spotify Dashboard

Clear token cache
GET /logout

Current user
GET /whoami

Devices (Spotify Connect)
GET /devices

Playback status
GET /status

Play (main endpoint)
POST /play Content-Type: application/json
Body:

{ "command": "play \"Song A\" \"Song B\" in loop till 15 minutes on iPhone" }


Command format

Put each title inside quotes.

Optional “- Artist”.

Optional device after on (defaults to your active device).

Time can be minutes (5, 10 minutes) or clock time (11:30 pm).

Pattern:

play "Song A" ["Song B" ...] in loop till <time> [on <device-name>]

Examples

PowerShell

# English pop
$body = @{ command = 'play "Flowers" "As It Was" "Levitating" in loop till 15 minutes on iPhone' } | ConvertTo-Json
Invoke-RestMethod -Method Post https://<your-service>.onrender.com/play -ContentType 'application/json' -Body $body

# Telugu set
$body = @{ command = 'play "Butta Bomma" "Samajavaragamana" "Ramuloo Ramulaa" "Inkem Inkem Inkem Kaavaale" "Naatu Naatu" in loop till 30 minutes on iPhone' } | ConvertTo-Json
Invoke-RestMethod -Method Post https://<your-service>.onrender.com/play -ContentType 'application/json' -Body $body


curl

curl -X POST https://<your-service>.onrender.com/play \
  -H "Content-Type: application/json" \
  -d '{"command":"play \"Kesariya\" \"Tum Hi Ho\" in loop till 20 minutes on iPhone"}'

Siri Shortcut (voice control)

Goal: “Hey Siri, Loop Agent… your command here”

Open Shortcuts → + → name it Loop Agent.

Add Action → Get Contents of URL

Method: GET

URL: https://<your-service>.onrender.com/health (pre-wake the free Render instance)

Add Action → Dictate Text

Prompt: Say your Loop Agent command

Add Action → Get Contents of URL

Method: POST

URL: https://<your-service>.onrender.com/play

Headers: Content-Type = application/json

Request Body: JSON → Dictionary

Key: command → Value: Dictated Text

(Optional) Show Result to see the server reply.

Now say:
“Hey Siri, Loop Agent — play ‘Levitating’ ‘As It Was’ in loop till 10 minutes on iPhone.”

Troubleshooting
Symptom / Code	What it means	Fix
401 Unauthorized	Not authorized with Spotify	Open /login once (Render) or /login on local
404 with message	A track title didn’t resolve	Try titles only (without - Artist) or copy exact Spotify title
409 Conflict	No active Spotify device	Open Spotify on iPhone/PC (same account), play/pause once, try again
Random 500	Title typo, or momentary Spotify hiccup	Try 1–2 songs first; verify titles; re-run
First call slow / 502	Free Render cold start	Always hit /health first (Shortcut step #2)
Nothing plays on iPhone	Device not selected	Call /devices to confirm, or add on iPhone to the command

Tips

Titles-only are most reliable.

Keep your Spotify app open once so the device appears under /devices.

Avoid spamming /play back-to-back; wait a second between calls.

What’s inside (server_render.py, quick design)

Parses your command → titles, end time, target device

Searches Spotify → creates a tiny private playlist (each track added once)

Transfers playback to your device, disables shuffle, sets repeat=context, starts at track 0

Background thread pauses playback when time is up

Fast responses and fewer rate-limits (no 1000-item playlist spam)

Requirements

Spotify account (Premium recommended for playback control)

Spotify Developer app with redirect URI set:

Local: http://localhost:5055/callback

Render: https://<your-service>.onrender.com/callback

Environment variables

SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=https://<your-service>.onrender.com/callback  # or http://localhost:5055/callback for local
# optional
DEFAULT_DEVICE_NAME=iPhone
OAUTH_CACHE_PATH=/tmp/loop-agent-cache.json
PORT=5055  # local only


requirements.txt 
flask
spotipy
python-dateutil
gunicorn


Render settings

Build: pip install -r requirements.txt

Start: gunicorn server_render:app --workers 2 --timeout 120 --log-level info

Health Check Path: /health

Env Vars: as above

## License

This project is licensed under the [MIT License](LICENSE) - you are free to use, modify, and distribute it with attribution.