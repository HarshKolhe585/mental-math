MENTAL MATH - complete website (frontend + backend in this one folder)
=======================================================================

START IT (Windows)
  Double-click  run.bat
  The first run sets itself up (about a minute), then your browser opens at
  http://127.0.0.1:8000.  Keep the black window open while you play.
  To stop: press Ctrl+C in that window, or close it.

START IT (manual / VS Code terminal, from inside THIS folder)
  python -m pip install -r requirements.txt
  python -m uvicorn main:app --reload
  (Do not use VS Code's Run button - it can't start a web server.)

START IT (Mac / Linux)
  bash run.sh

WHAT IS IN THIS FOLDER
  main.py           BACKEND  - accounts, guest mode, game rules, leaderboard (FastAPI)
  engine.py         BACKEND  - question generation, series/chains, answer parsing
  requirements.txt  the Python packages needed
  static/index.html FRONTEND - the whole web page (design, timer, voice, themes)
  static/img/       optional artwork for the level-1..7 partner (see README inside)
  run.bat / run.sh  one-click launchers
  mathgame.db       created automatically on first run: accounts and scores

NOTES
  - Use Chrome or Edge for voice mode; allow the microphone when asked.
  - To reset all accounts and scores, stop the server and delete mathgame.db.
  - Keep this folder somewhere permanent (not Downloads).

GUEST LIMIT
  Guests can play a limited number of sessions before they must create an account.
  After signing up (or signing in) their game continues exactly where it stopped.
  Environment variables (set before starting the server):
    GUEST_FREE_TRIALS    How many full sessions a guest can play (default: 2).
    GUEST_GRACE_ANSWERS  Extra answers allowed in the next session before the
                         sign-up wall appears (default: 5; set to 0 to block
                         immediately when free trials are exhausted).
    COOKIE_SECURE        Set to 1 when serving over HTTPS (Render, etc.)
                         so session cookies use Secure + SameSite=None.
    GOOGLE_CLIENT_ID     Google Cloud OAuth 2.0 Web Client ID for verified
                         Google Sign-In (from console.cloud.google.com).
