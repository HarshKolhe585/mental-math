"""
Mental Math web server (FastAPI + SQLite).
Run:  uvicorn main:app --reload      then open http://127.0.0.1:8000

Design notes
------------
* The SERVER generates questions, checks answers and times them, so scores
  can't be faked from the browser.
* Every visitor is a guest automatically (cookie session). Creating an account
  upgrades that same guest row, so earlier progress carries over.
* Game state lives in the database, not in memory, so any number of server
  copies can run behind a load balancer (swap SQLite for PostgreSQL to scale).
* Endpoints are plain `def`, so FastAPI runs them in a thread pool and the
  CPU-heavy password hashing never blocks other players.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import OPERATIONS, GameSession, Question, parse_spoken_number

BASE = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE / "mathgame.db"))
SECURE_COOKIE = os.environ.get("COOKIE_SECURE") == "1"  # set to 1 when served over HTTPS
FREE_TRIALS   = int(os.environ.get("GUEST_FREE_TRIALS", 2))
GRACE_ANSWERS = int(os.environ.get("GUEST_GRACE_ANSWERS", 5))
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
VALID_OPS = set(OPERATIONS.values())

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, pw_hash TEXT,
  is_guest INTEGER DEFAULT 1, best_separate INTEGER DEFAULT 0, best_mixed INTEGER DEFAULT 0,
  total_correct INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS games(
  id TEXT PRIMARY KEY, user_id INTEGER, mode TEXT, op TEXT, guided INTEGER,
  q_text TEXT, q_answer INTEGER, correct INTEGER, total INTEGER, streak INTEGER,
  best INTEGER, asked_at REAL);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # many readers + one writer without blocking
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_columns(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "google_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN google_id TEXT")


with db() as _c:
    _c.executescript(SCHEMA)
    ensure_columns(_c)

# ----------------------------------------------------------------------------
# Passwords and sessions
# ----------------------------------------------------------------------------


def hash_pw(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def check_pw(password, stored):
    salt_hex, _ = stored.split("$")
    return hmac.compare_digest(hash_pw(password, bytes.fromhex(salt_hex)), stored)


def new_session(conn, response, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, time.time()))
    samesite = "none" if SECURE_COOKIE else "lax"
    response.set_cookie("sid", token, httponly=True, samesite=samesite,
                        secure=SECURE_COOKIE, max_age=30 * 86400)


def get_user(conn, request, response):
    """Return the logged-in user, or silently create a guest (guest mode)."""
    token = request.cookies.get("sid")
    if token:
        row = conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                           "WHERE s.token = ?", (token,)).fetchone()
        if row:
            return row
    uid = conn.execute("INSERT INTO users(created) VALUES (?)", (time.time(),)).lastrowid
    new_session(conn, response, uid)
    return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def public(u):
    return {"username": u["username"], "guest": bool(u["is_guest"]),
            "best": max(u["best_separate"], u["best_mixed"]),
            "email": u["email"] if "email" in u.keys() else None}


def signup_wall(conn, user, game_id=None):
    """Return True if this guest has exhausted free trials + grace answers."""
    if not user["is_guest"]:
        return False
    rows = conn.execute("SELECT id, total FROM games WHERE user_id=? ORDER BY rowid",
                        (user["id"],)).fetchall()
    grace_total = sum(r["total"] for r in rows[FREE_TRIALS:])
    walled = grace_total >= GRACE_ANSWERS
    if game_id is None:  # starting a new session
        return len(rows) >= FREE_TRIALS and walled
    # answering inside a session
    idx = 0
    for i, r in enumerate(rows):
        if r["id"] == game_id:
            idx = i
            break
    return idx >= FREE_TRIALS and walled


app = FastAPI(title="Mental Math")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Creds(BaseModel):
    username: str
    password: str


def validate(c):
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", c.username):
        raise HTTPException(422, "Username: 3-20 letters, numbers or underscores")
    if len(c.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")


@app.get("/api/me")
def me(request: Request, response: Response):
    with db() as c:
        u = get_user(c, request, response)
        trials_used = c.execute("SELECT COUNT(*) AS n FROM games WHERE user_id=?",
                                (u["id"],)).fetchone()["n"]
        d = public(u)
        d["free_trials"] = FREE_TRIALS
        d["trials_used"] = trials_used
        d["google_client_id"] = GOOGLE_CLIENT_ID
        return d


@app.post("/api/register")
def register(creds: Creds, request: Request, response: Response):
    validate(creds)
    with db() as c:
        u = get_user(c, request, response)
        if not u["is_guest"]:
            raise HTTPException(400, "Log out before creating another account")
        try:  # upgrade the guest in place: its history and streaks are kept
            c.execute("UPDATE users SET username=?, pw_hash=?, is_guest=0 WHERE id=?",
                      (creds.username, hash_pw(creds.password), u["id"]))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That username is taken")
        return public(c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())


@app.post("/api/login")
def login(creds: Creds, request: Request, response: Response):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (creds.username,)).fetchone()
        if not u or not check_pw(creds.password, u["pw_hash"]):
            raise HTTPException(401, "Wrong username or password")
        # If the current session belongs to a different guest, merge its games
        guest = get_user(c, request, response)
        if guest["is_guest"] and guest["id"] != u["id"]:
            c.execute("UPDATE games SET user_id=? WHERE user_id=?", (u["id"], guest["id"]))
            c.execute("UPDATE users SET best_separate=MAX(best_separate,?), "
                      "best_mixed=MAX(best_mixed,?), "
                      "total_correct=total_correct+? WHERE id=?",
                      (guest["best_separate"], guest["best_mixed"],
                       guest["total_correct"], u["id"]))
        new_session(c, response, u["id"])
        return public(u)


@app.post("/api/logout")
def logout(request: Request, response: Response):
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (request.cookies.get("sid"),))
    response.delete_cookie("sid")
    return {"ok": True}


class GoogleAuthReq(BaseModel):
    credential: str


@app.post("/api/auth/google")
def auth_google(req: GoogleAuthReq, request: Request, response: Response):
    if not req.credential:
        raise HTTPException(400, "Missing Google credential token")

    # Verify ID token using Google TokenInfo API
    token_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={req.credential}"
    try:
        ur = urllib.request.Request(token_url, headers={"User-Agent": "MentalMath"})
        with urllib.request.urlopen(ur, timeout=10) as r:
            token_data = json.loads(r.read().decode("utf-8"))
    except Exception:
        raise HTTPException(401, "Google token verification failed")

    if GOOGLE_CLIENT_ID and token_data.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "Google Client ID mismatch")

    if token_data.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "Invalid token issuer")

    if str(token_data.get("email_verified", "")).lower() != "true":
        raise HTTPException(400, "Google email not verified")

    google_id = token_data["sub"]
    email = token_data.get("email", "").lower()
    raw_name = token_data.get("name", "").strip() or email.split("@")[0]

    with db() as c:
        current_guest = get_user(c, request, response)

        # Check if an existing registered user has this google_id or verified email
        existing = c.execute("SELECT * FROM users WHERE google_id=? OR (email=? AND email IS NOT NULL AND is_guest=0)",
                             (google_id, email)).fetchone()

        if existing:
            # Merge current guest progress into existing account if different
            if current_guest["is_guest"] and current_guest["id"] != existing["id"]:
                c.execute("UPDATE games SET user_id=? WHERE user_id=?", (existing["id"], current_guest["id"]))
                c.execute("UPDATE users SET best_separate=MAX(best_separate,?), "
                          "best_mixed=MAX(best_mixed,?), "
                          "total_correct=total_correct+? WHERE id=?",
                          (current_guest["best_separate"], current_guest["best_mixed"],
                           current_guest["total_correct"], existing["id"]))
            new_session(c, response, existing["id"])
            return public(existing)

        # New user: upgrade guest row in place so existing streak/games are kept
        clean_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_name)[:20]
        if len(clean_name) < 3:
            clean_name = "user_" + secrets.token_hex(3)
        username = clean_name

        # Ensure unique username
        dup = c.execute("SELECT id FROM users WHERE username=? AND id!=?",
                        (username, current_guest["id"])).fetchone()
        if dup:
            suffix = "_" + secrets.token_hex(2)
            username = username[:20 - len(suffix)] + suffix

        c.execute("UPDATE users SET username=?, is_guest=0, email=?, google_id=? WHERE id=?",
                  (username, email, google_id, current_guest["id"]))
        updated = c.execute("SELECT * FROM users WHERE id=?", (current_guest["id"],)).fetchone()
        return public(updated)


# ----------------------------------------------------------------------------
# Game (all rules enforced here, using the same engine as the desktop app)
# ----------------------------------------------------------------------------


class StartReq(BaseModel):
    mode: str
    op: str = "add"
    guided: bool = True


class AnswerReq(BaseModel):
    game_id: str
    answer: Optional[int] = None  # typed answer
    text: Optional[str] = None    # raw speech transcript from the browser


class RestartReq(BaseModel):
    game_id: str


def stats(s):
    return {"correct": s.correct, "total": s.total, "streak": s.streak, "best": s.best}


def load(conn, game_id, user):
    """Rebuild a GameSession from its database row."""
    g = conn.execute("SELECT * FROM games WHERE id=? AND user_id=?",
                     (game_id, user["id"])).fetchone()
    if not g:
        raise HTTPException(404, "Game not found")
    s = GameSession(g["mode"], g["op"], bool(g["guided"]))
    s.question = Question(g["q_text"], g["q_answer"])
    s.correct, s.total, s.streak, s.best = g["correct"], g["total"], g["streak"], g["best"]
    return g, s


def save(conn, game_id, s):
    conn.execute("UPDATE games SET q_text=?, q_answer=?, correct=?, total=?, streak=?, best=?, "
                 "asked_at=? WHERE id=?",
                 (s.question.text, s.question.answer, s.correct, s.total, s.streak, s.best,
                  time.time(), game_id))


@app.post("/api/game/start")
def start(req: StartReq, request: Request, response: Response):
    if req.mode not in ("separate", "mixed") or req.op not in VALID_OPS:
        raise HTTPException(422, "Invalid mode or operation")
    s = GameSession(req.mode, req.op, req.guided)
    q = s.next_question()
    gid = secrets.token_urlsafe(12)
    with db() as c:
        u = get_user(c, request, response)
        if signup_wall(c, u):
            raise HTTPException(403, "signup_required")
        c.execute("INSERT INTO games(id,user_id,mode,op,guided,q_text,q_answer,correct,total,"
                  "streak,best,asked_at) VALUES (?,?,?,?,?,?,?,0,0,0,0,?)",
                  (gid, u["id"], req.mode, req.op, int(req.guided), q.text, q.answer, time.time()))
    return {"game_id": gid, "question": q.text, "prev": None,
            "new_series": True, "stats": stats(s)}


@app.post("/api/game/answer")
def answer(req: AnswerReq, request: Request, response: Response):
    value = req.answer if req.answer is not None else parse_spoken_number(req.text or "")
    if value is None:  # noise or unintelligible speech: ignore, keep the question
        return {"ignored": True}
    with db() as c:
        u = get_user(c, request, response)
        if signup_wall(c, u, req.game_id):
            raise HTTPException(403, "signup_required")
        g, s = load(c, req.game_id, u)
        elapsed = time.time() - g["asked_at"]  # measured on the server
        true_answer = g["q_answer"]
        ok = s.submit(value)
        q = s.next_question()  # chains continue from the TRUE value
        save(c, req.game_id, s)

        col = "best_" + g["mode"]  # mode was validated at game start
        if s.streak > u[col]:
            c.execute(f"UPDATE users SET {col}=? WHERE id=?", (s.streak, u["id"]))
        if ok:
            c.execute("UPDATE users SET total_correct = total_correct + 1 WHERE id=?", (u["id"],))

    guided_chain = g["mode"] == "mixed" and bool(g["guided"])
    reveal = g["mode"] == "separate" or bool(g["guided"])  # Memory mode gives no hints
    return {"ok": ok, "your": value, "elapsed": round(elapsed, 2),
            "correct": true_answer if (not ok and reveal) else None,
            "question": q.text, "new_series": s.new_series,
            "prev": true_answer if guided_chain else None, "stats": stats(s)}


@app.post("/api/game/restart")
def restart(req: RestartReq, request: Request, response: Response):
    with db() as c:
        u = get_user(c, request, response)
        _, s = load(c, req.game_id, u)
        s.reset_chain()
        q = s.next_question()
        save(c, req.game_id, s)
    return {"question": q.text, "prev": None, "new_series": True, "stats": stats(s)}


# ----------------------------------------------------------------------------
# Leaderboard: longest streak of correct answers (registered players only)
# ----------------------------------------------------------------------------
@app.get("/api/leaderboard")
def leaderboard(request: Request, response: Response, mode: str = "all"):
    expr = {"all": "MAX(best_separate, best_mixed)", "separate": "best_separate",
            "mixed": "best_mixed"}.get(mode)
    if not expr:
        raise HTTPException(422, "Invalid board")
    with db() as c:
        u = get_user(c, request, response)
        rows = c.execute(f"SELECT username, {expr} AS best FROM users "
                         f"WHERE is_guest=0 AND {expr} > 0 "
                         "ORDER BY best DESC, total_correct DESC, id LIMIT 20").fetchall()
        mine = c.execute(f"SELECT {expr} AS best FROM users WHERE id=?", (u["id"],)).fetchone()["best"]
        rank = None
        if not u["is_guest"] and mine > 0:
            rank = c.execute(f"SELECT COUNT(*) + 1 AS r FROM users "
                             f"WHERE is_guest=0 AND {expr} > ?", (mine,)).fetchone()["r"]
    return {"rows": [{"rank": i + 1, "username": r["username"], "best": r["best"]}
                     for i, r in enumerate(rows)],
            "me": {"best": mine, "rank": rank, "guest": bool(u["is_guest"])}}


if (BASE / "static").is_dir():
    app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
elif (BASE / "index.html").is_file():
    from fastapi.responses import FileResponse
    @app.get("/")
    def serve_index():
        return FileResponse(BASE / "index.html")
else:
    @app.get("/")
    def health():
        return {"status": "ok", "message": "Mental Math API is running"}
