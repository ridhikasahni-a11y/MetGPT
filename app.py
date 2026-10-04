# -*- coding: utf-8 -*-
"""
MetGPT - AI Weather Assistant
Flask app using Gemini 2.5 Flash Lite + LocationIQ + Met Eireann Open API
"""

from dotenv import load_dotenv
load_dotenv()
import socket
from urllib3.util import connection

def allowed_gai_family():
    return socket.AF_INET
connection.allowed_gai_family = allowed_gai_family

import os, json, re, logging, secrets, sqlite3, threading, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from time import time

import requests
import feedparser
from flask import Flask, g, render_template, request, session, redirect, url_for, abort, jsonify
from flask_session import Session
from cachetools import TTLCache, cached
from haversine import haversine, Unit
import google.generativeai as genai

# ─── Setup ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LOCATIONIQ_KEY = os.getenv("LOCATIONIQ_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable not set")
if not LOCATIONIQ_KEY:
    raise RuntimeError("LOCATIONIQ_KEY environment variable not set")

genai.configure(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-3.5-flash-lite"

app = Flask(__name__)
# Use a stable secret from .env so logins survive server restarts.
# Falls back to a random key if not set (sessions reset on restart).
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(32)
_EPHEMERAL_SECRET = not os.getenv("FLASK_SECRET_KEY")
# Cookie settings — needed for sessions to work over HTTPS (e.g. on Railway)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Only require HTTPS-only cookies in production (Railway sets RAILWAY_ENVIRONMENT)
app.config["SESSION_COOKIE_SECURE"] = os.getenv("RAILWAY_ENVIRONMENT") is not None
# Keep people logged in across browser restarts. Without this the session
# cookie is a browser-session cookie and every returning user looks brand new.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)


@app.before_request
def _make_session_permanent():
    session.permanent = True

# Session backend: use Flask's built-in signed-cookie sessions in production
# (works across gunicorn workers with no shared filesystem). Locally we can
# use filesystem sessions for larger chat histories.
USE_COOKIE_SESSIONS = os.getenv("RAILWAY_ENVIRONMENT") is not None
if not USE_COOKIE_SESSIONS:
    app.config["SESSION_TYPE"] = "filesystem"
    SESSION_DIR = "/tmp/flask_session_metgpt"
    os.makedirs(SESSION_DIR, exist_ok=True)
    app.config["SESSION_FILE_DIR"] = SESSION_DIR
    app.config["SESSION_PERMANENT"] = True
    Session(app)
# else: default Flask cookie sessions (no Session(app) call needed)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

if _EPHEMERAL_SECRET:
    logger.warning("FLASK_SECRET_KEY is not set — a random key is being used, so every "
                   "restart will log all users out. Set it in .env for persistent logins.")

# ─── Authentication ───────────────────────────────────────────────────────────
# Users are defined in .env as:  METGPT_USERS=alice:pass1,bob:pass2
import hmac

def _parse_users():
    raw = os.getenv("METGPT_USERS", "")
    users = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            name, pwd = pair.split(":", 1)
            name, pwd = name.strip(), pwd.strip()
            if name and pwd:
                users[name] = pwd
    return users

USERS = _parse_users()
if not USERS:
    logger.warning("No METGPT_USERS set in .env — login will reject everyone. "
                   "Add e.g. METGPT_USERS=alice:secret,bob:hunter2")

def check_credentials(username, password):
    expected = USERS.get(username)
    if expected is None:
        return False
    # constant-time comparison to avoid timing attacks
    return hmac.compare_digest(expected, password)

def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in_user"):
            # API routes return 401 JSON; page routes redirect to login
            if request.path.startswith(("/chat", "/settings", "/clear")):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper

# Global caches
# Forecasts expire after an hour so a long-running server never serves a
# stale forecast (a plain dict here kept forecasts until the next restart).
FORECAST_CACHE_TTL = 3600
_cached_locations = TTLCache(maxsize=256, ttl=FORECAST_CACHE_TTL)
_warnings_cache = None
_warnings_last_fetched = None

# ─── Rate limiting ─────────────────────────────────────────────────────────────

RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_CALLS = 15
_calls = {}
_calls_lock = threading.Lock()
_daily_calls = 0
_daily_reset = time()
_daily_lock = threading.Lock()
DAILY_MAX_CALLS = 500

def rate_limit(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        sid = session.get("sid") or request.remote_addr
        now = time()
        with _calls_lock:
            hits = _calls.setdefault(sid, [])
            hits[:] = [t for t in hits if now - t < RATE_LIMIT_WINDOW]
            if len(hits) >= RATE_LIMIT_MAX_CALLS:
                abort(429, "Rate limit exceeded. Please wait a minute.")
            hits.append(now)
        return func(*args, **kwargs)
    return wrapper

def increment_daily():
    global _daily_calls, _daily_reset
    with _daily_lock:
        if time() - _daily_reset > 86400:
            _daily_calls = 0
            _daily_reset = time()
        if _daily_calls >= DAILY_MAX_CALLS:
            abort(429, "Daily quota reached. Try again tomorrow.")
        _daily_calls += 1

# ─── LLM ──────────────────────────────────────────────────────────────────────

TPM_LIMIT = 250_000
_tokens_used = 0
_token_reset = time()
_advice_cache = {}
_CACHE_TTL = 1800


def call_llm(prompt: str) -> str:
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        history = session.get("chat_history", [])
        formatted = []
        for msg in history:
            if msg["role"] == "user":
                formatted.append({"role": "user", "parts": [msg["content"]]})
            elif msg["role"] == "assistant":
                formatted.append({"role": "model", "parts": [msg["content"]]})

        full_prompt = (
            "Conversation history:\n"
            + (str(formatted) if formatted else "None")
            + f"\n\nUser: {prompt}\n\nAssistant:"
        )
        response = model.generate_content(full_prompt)
        return response.text.strip()
    except Exception as e:
        logger.error("LLM error: %s", e)
        return "I'm having trouble thinking right now. Please try again."


def call_llm_throttled(prompt: str) -> str:
    global _tokens_used, _token_reset
    now = time()
    if now - _token_reset > 60:
        _tokens_used = 0
        _token_reset = now
    est = int(len(prompt.split()) * 1.5)
    if _tokens_used + est > TPM_LIMIT:
        return "Token quota exceeded. Try again in a minute."
    increment_daily()
    _tokens_used += est
    return call_llm(prompt)


def extract_json_block(text):
    cleaned = re.sub(r"```(?:json)?\n?|\n?```", "", text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return None


# ─── Location & date extraction ───────────────────────────────────────────────

def extract_locations_dates(user_text, history):
    today = datetime.now()
    history_ctx = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in history[-10:]
    ) or "None"
    prompt = f"""You are a JSON extractor for weather queries.
Today is {today.strftime('%Y-%m-%d')} ({today.strftime('%A')}).

Extract location and date references from the CURRENT user message only.
Return ONLY a JSON list of objects, each with:
  - "location": a place name ONLY if the word is genuinely an Irish place (city, town, village, county, area). Set it to null if the word is a time reference, an abbreviation, a greeting, or anything that is not a real place. NEVER infer or copy a location from the history.
  - "date": "YYYY-MM-DD" or null

Important: time-related words and their abbreviations (for example tomorrow, tom, tmrw, tmr, tonight, today, tdy, this evening, the weekend) are DATES, never locations. If you are unsure whether a word is a place, set location to null rather than guessing.

Date rules (apply to the CURRENT message only):
- "this week" / "coming week" / "next 7 days" / "the week" -> one object per day for the next 7 days
- "this weekend" / "weekend" -> the upcoming Saturday and Sunday
- "next few days" -> the next 3 days
- any wording meaning the next day (tomorrow, tom, tmrw, 2moro, etc.) -> tomorrow's date only
- any wording meaning the current day (today, now, tonight, tdy) -> today's date only
- a specific weekday or calendar date -> that single date
- if the current message mentions no date at all -> a single object with date = today's date

Use the history ONLY to understand context. NEVER add extra dates or locations from the history.
No commentary.

History (context only):
{history_ctx}

Current user message: {user_text}
Output:"""
    raw = call_llm_throttled(prompt)
    parsed = extract_json_block(raw)
    if not isinstance(parsed, list):
        return []
    results = []
    seen = set()
    for e in parsed:
        loc = (e.get("location") or "").strip() or None
        date = (e.get("date") or "").strip() or today.strftime("%Y-%m-%d")
        try:
            datetime.fromisoformat(date)
        except Exception:
            date = today.strftime("%Y-%m-%d")
        key = (loc, date)
        if key not in seen:
            seen.add(key)
            results.append({"location": loc, "date": date})
    logger.info("Extracted %d candidate(s): %s", len(results), results)
    return results


def extract_activities(user_text, history):
    history_ctx = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in history[-4:]
    )
    prompt = f"""Extract outdoor/travel activities from the user text and history.
Return ONLY a JSON list of strings. If none, return [].

History:
{history_ctx}

User: {user_text}
Output:"""
    raw = call_llm_throttled(prompt)
    parsed = extract_json_block(raw)
    return parsed if isinstance(parsed, list) and all(isinstance(i, str) for i in parsed) else []


def classify_query(user_text, history):
    hist = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in history[-10:]
    ) or "(none)"
    prompt = f"""You classify messages sent to an Irish weather assistant.

Conversation so far (most recent last):
{hist}

New message: "{user_text}"

Classify the NEW message as exactly one word:
- "followup"   = a weather question that builds on the previous weather request (same place/activity, or just changes the day, e.g. "and tomorrow?", "what about the weekend?")
- "standalone" = a new weather request (mentions a new place, activity, or starts a fresh weather question)
- "junk"       = NOT a weather question at all (a simple greeting is fine, but coding help, maths, general chit-chat, or anything unrelated to weather is junk)

Reply with exactly one word: followup, standalone, or junk."""
    raw = call_llm_throttled(prompt).strip().lower()
    return raw if raw in {"followup", "standalone", "junk"} else "standalone"


# ─── Geocoding ────────────────────────────────────────────────────────────────

_geo_cache = TTLCache(maxsize=500, ttl=3600)


@cached(_geo_cache)
def geocode_location(name: str):
    """Returns (lat, lon, display_name, ambiguous_counties).

    ambiguous_counties is None for a normal resolution. It's a sorted list of
    county names when the query name matches multiple distinct places (in
    different counties) with no single clearly-dominant match — e.g. "Glebe"
    or "Newcastle", which are shared by many unrelated townlands/villages
    across Ireland. In that case lat/lon/display_name are None and the
    caller should ask the user to disambiguate.
    """
    url = "https://us1.locationiq.com/v1/search.php"
    params = {
        "key": LOCATIONIQ_KEY,
        "q": f"{name}, Ireland",
        "format": "json",
        "countrycodes": "ie",
        "addressdetails": 1,
        "limit": 30,
    }

    # Only accept real settlements / administrative areas / notable natural
    # features — not businesses, shops, restaurants, etc. This stops "delhi"
    # matching "Delhi Rasoi", while still allowing places like beaches/bays
    # that people ask about weather for (e.g. "Brittas Bay").
    ALLOWED_CLASSES = {"place", "boundary"}
    ALLOWED_TYPES = {
        "city", "town", "village", "hamlet", "suburb", "neighbourhood",
        "locality", "municipality", "county", "state", "administrative",
        "island", "townland", "beach", "bay",
    }
    # Real, recognisable named settlements — as opposed to administrative
    # artifacts like electoral divisions/townlands, which share generic
    # names (e.g. "Glebe") across dozens of unrelated places.
    SETTLEMENT_TYPES = {"city", "town", "village", "hamlet", "suburb"}
    # A match this much less important than the top match is considered a
    # long tail, not a genuine competing candidate for ambiguity purposes.
    # Only used as a fallback when there's no clash between real settlements.
    AMBIGUITY_IMPORTANCE_MARGIN = 0.03

    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        results = resp.json()
        if not isinstance(results, list):
            return None, None, name, None

        accepted = []
        for r in results:
            r_class = (r.get("class") or "").lower()
            r_type = (r.get("type") or "").lower()
            # Accept only genuine settlements / admin areas / allowed natural features
            if r_class in ALLOWED_CLASSES or r_type in ALLOWED_TYPES:
                try:
                    lat = float(r["lat"])
                    lon = float(r["lon"])
                except Exception:
                    continue
                accepted.append({
                    "raw": r,
                    "lat": lat,
                    "lon": lon,
                    "importance": float(r.get("importance") or 0),
                })

        if accepted:
            # If the caller's own text already names one of the candidate
            # counties (e.g. "Newcastle, County Dublin" answering an earlier
            # disambiguation prompt), trust it instead of re-asking — filter
            # down to that county before running the ambiguity checks below.
            # LocationIQ's search doesn't strictly filter by every word in
            # the query, so other counties' matches (Tipperary, Wicklow) can
            # still come back even when the county was explicitly given.
            name_lower = name.lower()
            candidate_counties = set()
            for c in accepted:
                county = (c["raw"].get("address", {}).get("county")
                          or c["raw"].get("address", {}).get("state"))
                if county:
                    candidate_counties.add(county)
            named_counties = [co for co in candidate_counties if co.lower() in name_lower]
            if len(named_counties) == 1:
                chosen_county = named_counties[0]
                accepted = [
                    c for c in accepted
                    if (c["raw"].get("address", {}).get("county")
                        or c["raw"].get("address", {}).get("state")) == chosen_county
                ]

            # Tier 1: two or more genuine named settlements (town/village/suburb/etc.)
            # in different counties are always ambiguous, no matter how much bigger
            # one is than the other — e.g. Blackrock, Co. Dublin vs Blackrock, Co.
            # Cork are both real places; we shouldn't silently pick the bigger one.
            settlement_counties = []
            seen_settlement_counties = set()
            for c in accepted:
                r = c["raw"]
                if (r.get("class") or "").lower() == "place" and (r.get("type") or "").lower() in SETTLEMENT_TYPES:
                    county = r.get("address", {}).get("county") or r.get("address", {}).get("state")
                    if county and county not in seen_settlement_counties:
                        seen_settlement_counties.add(county)
                        settlement_counties.append(county)
            if len(settlement_counties) > 1:
                logger.info("Geocode ambiguous '%s' across settlement counties: %s", name, settlement_counties)
                return None, None, name, sorted(settlement_counties)

            # Tier 2: fallback for names with no distinct settlement match at all
            # (e.g. "Glebe", which only resolves to electoral-division boundaries) —
            # flag ambiguity when multiple counties are tied at the top importance band.
            top_importance = max(c["importance"] for c in accepted)
            top_tier = [c for c in accepted if c["importance"] >= top_importance - AMBIGUITY_IMPORTANCE_MARGIN]
            counties = []
            seen_counties = set()
            for c in top_tier:
                county = (c["raw"].get("address", {}).get("county")
                          or c["raw"].get("address", {}).get("state"))
                if county and county not in seen_counties:
                    seen_counties.add(county)
                    counties.append(county)
            if len(counties) > 1:
                logger.info("Geocode ambiguous '%s' across counties: %s", name, counties)
                return None, None, name, sorted(counties)

            # Once we know it's not ambiguous, prefer a genuine settlement match
            # (town/village/etc.) over a same-county administrative/civil-parish
            # boundary, even if the boundary entry scores higher on importance —
            # otherwise a real, well-known village can lose to an obscure legal
            # boundary that happens to be better-tagged in OSM (e.g. "Kilmore"
            # was resolving to a County Cavan civil parish instead of the
            # well-known Kilmore Quay village in County Wexford).
            settlement_matches = [
                sc for sc in accepted
                if (sc["raw"].get("class") or "").lower() == "place"
                and (sc["raw"].get("type") or "").lower() in SETTLEMENT_TYPES
            ]
            c = max(settlement_matches, key=lambda sc: sc["importance"]) if settlement_matches else accepted[0]
            r, lat, lon = c["raw"], c["lat"], c["lon"]
            # Prefer a clean settlement name from address details if present
            addr = r.get("address", {})
            clean = (addr.get("city") or addr.get("town") or addr.get("village")
                     or addr.get("hamlet") or addr.get("suburb") or addr.get("county"))
            display = clean or r.get("display_name", name)
            logger.info("Geocoded '%s' -> %s (%s/%s)", name, display, r.get("class"), r.get("type"))
            return lat, lon, display, None

        # Fallback: if the query itself already looks like a resolved place name
        # (contains a comma, e.g. "Dublin, Leinster, Ireland" reused from a
        # previous turn) and LocationIQ returned any Irish result, accept the
        # first one. This avoids rejecting already-geocoded display names.
        if "," in name and results:
            r = results[0]
            addr = r.get("address", {})
            if (addr.get("country_code") or "").lower() == "ie":
                lat = float(r["lat"])
                lon = float(r["lon"])
                clean = (addr.get("city") or addr.get("town") or addr.get("village")
                         or addr.get("hamlet") or addr.get("suburb") or addr.get("county")
                         or r.get("display_name", name))
                logger.info("Geocoded (fallback) '%s' -> %s", name, clean)
                return lat, lon, clean, None

        # Nothing acceptable found (e.g. only a business matched)
        logger.info("Geocode rejected '%s' — no settlement match in Ireland", name)
    except Exception as e:
        logger.error("LocationIQ error for '%s': %s", name, e)
    return None, None, name, None


def reverse_geocode(lat, lon):
    """Reverse-geocode coordinates to a place name. Returns (place, country_code)."""
    url = "https://us1.locationiq.com/v1/reverse.php"
    params = {"key": LOCATIONIQ_KEY, "lat": lat, "lon": lon, "format": "json"}
    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        addr = data.get("address", {})
        country_code = addr.get("country_code")
        place = (addr.get("city") or addr.get("town") or addr.get("village")
                 or addr.get("suburb") or addr.get("county") or data.get("display_name"))
        return place, country_code
    except Exception as e:
        logger.error("Reverse geocode error: %s", e)
        return None, None


def get_ip_location():
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() if forwarded else request.remote_addr
    if ip.startswith("127.") or ip == "::1":
        return None, None, None
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}", timeout=3)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            city = data.get("city")
            region = data.get("regionName")
            country = data.get("country")
            name = f"{city}, {region}, {country}" if city else "your location"
            return data.get("lat"), data.get("lon"), name
    except Exception as e:
        logger.error("IP geo error: %s", e)
    return None, None, None


# ─── Display rounding ─────────────────────────────────────────────────────────
# A forecast should never be shown to more precision than it is actually
# accurate to. Everything the user or the LLM sees passes through these
# helpers. Raw values are kept internally only so that aggregates (e.g. the
# daily rainfall total) are computed before being rounded, not after.

def _half_up(v):
    """Round half away from zero (12.5 -> 13), not Python's banker's rounding
    (which gives 12). Half-up is what a reader expects of a forecast figure."""
    return int(Decimal(repr(float(v))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def round_temp(v):
    """Temperature to the nearest whole degree."""
    return None if v is None else _half_up(v)


def round_wind_ms(v):
    """Wind speed in m/s to the nearest whole number."""
    return None if v is None else _half_up(v)


def ms_to_kmh(v):
    """Wind speed in km/h to the nearest 5, converted from the raw m/s value."""
    return None if v is None else _half_up(v * 3.6 / 5.0) * 5


def ms_to_mph(v):
    """Wind speed in mph to the nearest 5, converted from the raw m/s value."""
    return None if v is None else _half_up(v * 2.23694 / 5.0) * 5


def round_rain(v):
    """Rainfall to the nearest mm once it is over 2 mm, otherwise to 0.1 mm."""
    if v is None:
        return None
    return float(_half_up(v)) if v >= 2 else float(_half_up(v * 10)) / 10.0


def round_pct(v, step=5):
    """Any percentage to the nearest `step` per cent."""
    return None if v is None else _half_up(v / float(step)) * step


def format_prob(v):
    """Probability to the nearest 5%, never claiming total certainty."""
    p = round_pct(v)
    if p is None:
        return None
    if p <= 0:
        return "<5%"
    if p >= 100:
        return ">95%"
    return "%d%%" % p


# ─── Per-user settings store ──────────────────────────────────────────────────
# Settings follow the person, not the browser. The session carries only the
# login and the conversation; everything the user configures lives here under
# their username, so it survives a browser restart and follows them to another
# device.
#
# NOTE: container filesystems are usually ephemeral. On Railway, point
# METGPT_DATA_DIR at a mounted volume or these settings reset on every deploy.

DATA_DIR = os.getenv("METGPT_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_DB = os.path.join(DATA_DIR, "settings.db")

TEMP_UNITS = ("c", "f")
WIND_UNITS = ("kmh", "ms", "mph", "kn", "bft")
RAIN_UNITS = ("mm", "in")

DEFAULT_SETTINGS = {
    "home_location": "",
    "home_lat": None,
    "home_lon": None,
    "home_display": "",
    "user_context": "",
    "answer_length": "detailed",
    "temp_unit": "c",
    "wind_units": ["kmh", "ms"],
    "rain_unit": "mm",
}


def _settings_db():
    conn = sqlite3.connect(SETTINGS_DB, timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        username   TEXT PRIMARY KEY,
        data       TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    return conn


def _clean_settings(raw):
    """Coerce whatever is stored into a valid settings dict."""
    s = dict(DEFAULT_SETTINGS)
    s["wind_units"] = list(DEFAULT_SETTINGS["wind_units"])
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
    if s.get("answer_length") not in ("short", "detailed"):
        s["answer_length"] = DEFAULT_SETTINGS["answer_length"]
    if s.get("temp_unit") not in TEMP_UNITS:
        s["temp_unit"] = DEFAULT_SETTINGS["temp_unit"]
    if s.get("rain_unit") not in RAIN_UNITS:
        s["rain_unit"] = DEFAULT_SETTINGS["rain_unit"]
    # Keep the canonical order rather than whatever order they were ticked in,
    # and never end up with no wind unit at all. The isinstance guard matters:
    # a bare string like "kmh" would otherwise pass the "in" test by substring
    # and silently become a one-unit selection.
    raw_wind = s.get("wind_units")
    if not isinstance(raw_wind, (list, tuple)):
        raw_wind = []
    picked = [u for u in WIND_UNITS if u in raw_wind]
    s["wind_units"] = picked or list(DEFAULT_SETTINGS["wind_units"])
    for k in ("home_location", "home_display", "user_context"):
        s[k] = (s.get(k) or "")
    return s


def load_settings(username):
    if not username:
        return _clean_settings(None)
    try:
        with _settings_db() as conn:
            row = conn.execute(
                "SELECT data FROM user_settings WHERE username = ?", (username,)).fetchone()
        return _clean_settings(json.loads(row[0]) if row else None)
    except Exception as e:
        logger.error("Could not load settings for %s: %s", username, e)
        return _clean_settings(None)


def save_settings(username, settings):
    if not username:
        return False
    try:
        with _settings_db() as conn:
            conn.execute(
                "INSERT INTO user_settings (username, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET data = excluded.data, "
                "updated_at = excluded.updated_at",
                (username, json.dumps(_clean_settings(settings)),
                 datetime.now(timezone.utc).isoformat()))
        return True
    except Exception as e:
        logger.error("Could not save settings for %s: %s", username, e)
        return False


def current_settings():
    """This request's settings for the logged-in user, read once per request."""
    if not hasattr(g, "metgpt_settings"):
        g.metgpt_settings = load_settings(session.get("logged_in_user", ""))
    return g.metgpt_settings


def update_settings(**changes):
    """Apply changes, persist them, and keep this request's copy in step."""
    s = dict(current_settings())
    s.update(changes)
    s = _clean_settings(s)
    save_settings(session.get("logged_in_user", ""), s)
    g.metgpt_settings = s
    return s


# ─── Units ────────────────────────────────────────────────────────────────────
# Each unit carries its own rounding step, chosen so the figure never claims
# more precision than a forecast can support: 5 for the coarse wind units,
# 1 for the fine ones, whole degrees for temperature either side.

TEMP_LABEL = {"c": "°C", "f": "°F"}
WIND_LABEL = {"kmh": "km/h", "ms": "m/s", "mph": "mph", "kn": "kn", "bft": "Beaufort"}
RAIN_LABEL = {"mm": "mm", "in": "in"}

# Upper bound of each Beaufort force in m/s (force 12 is open-ended).
BEAUFORT_MAX_MS = [0.5, 1.5, 3.3, 5.4, 7.9, 10.7, 13.8, 17.1, 20.7, 24.4, 28.4, 32.6]


def to_beaufort(ms):
    """Beaufort force from wind speed in m/s. Already a whole-number scale."""
    if ms is None:
        return None
    for force, upper in enumerate(BEAUFORT_MAX_MS):
        if ms < upper:
            return force
    return 12


def format_temp(celsius, unit="c"):
    """A temperature in the chosen unit, always to the whole degree."""
    if celsius is None:
        return None
    if unit == "f":
        return "%d°F" % _half_up(celsius * 9.0 / 5.0 + 32.0)
    return "%d°C" % _half_up(celsius)


def format_temp_range(tmin, tmax, unit="c"):
    """A min-max range in whole degrees, collapsed when rounding makes them equal."""
    a, b = format_temp(tmin, unit), format_temp(tmax, unit)
    if a is None and b is None:
        return "N/A"
    if a is None or b is None:
        return b if a is None else a
    if a == b:
        return a
    # "8–15°C", not "8°C–15°C"
    suffix = TEMP_LABEL[unit if unit in TEMP_LABEL else "c"]
    return "%s–%s" % (a[:-len(suffix)], b)


def format_wind(ms, units=None):
    """Wind speed in each chosen unit, e.g. '20 km/h · 5 m/s'."""
    if ms is None:
        return None
    units = [u for u in WIND_UNITS if u in (units or DEFAULT_SETTINGS["wind_units"])] \
        or list(DEFAULT_SETTINGS["wind_units"])
    parts = []
    for u in units:
        if u == "kmh":
            parts.append("%d km/h" % ms_to_kmh(ms))
        elif u == "ms":
            parts.append("%d m/s" % round_wind_ms(ms))
        elif u == "mph":
            parts.append("%d mph" % ms_to_mph(ms))
        elif u == "kn":                     # knots, to the nearest 5
            parts.append("%d kn" % (_half_up(ms * 1.94384 / 5.0) * 5))
        elif u == "bft":
            parts.append("F%d" % to_beaufort(ms))
    return " · ".join(parts)


def format_rain(mm, unit="mm"):
    """Rainfall in the chosen unit, at a step the forecast can actually support.

    In mm that is the nearest mm once over 2 mm, and 0.1 mm below it. Inches
    get the equivalent treatment: 2 mm is about 0.08 in, so above 0.1 in the
    step is 0.05 in (roughly 1 mm) and below it two decimal places.
    """
    if mm is None:
        return None
    if unit == "in":
        inches = mm / 25.4
        if inches >= 0.1:
            return "%.2f in" % (_half_up(inches / 0.05) * 0.05)
        return "%.2f in" % (_half_up(inches * 100) / 100.0)
    r = round_rain(mm)
    return "%d mm" % int(r) if r >= 2 or r == 0 else "%.1f mm" % r


# Roughly how long rain stays correlated with itself, in hours. A day is
# treated as about 24 / RAIN_BLOCK_HOURS independent weather episodes rather
# than 24 independent hours. Lower it to make the daily chance bolder, raise
# it to make it more conservative; at 24 it collapses to the wettest hour.
RAIN_BLOCK_HOURS = 6


def daily_rain_probability(hourly_rows):
    """The chance of rain at some point during the day.

    This used to be the mean of the hourly probabilities, which answers a
    different question: the average hourly chance is not the chance of rain at
    some point, and it understates it badly. A day of 24 hours each at 30%
    averages to 30%, but you would plainly expect rain at some stage.

    Averaging is wrong, and so is the opposite extreme of treating the hours as
    independent and combining them as 1 - prod(1 - p_i): rain is strongly
    autocorrelated, so those same 24 hours would come out at 99.98%, which is
    nonsense. So the day is split into blocks of RAIN_BLOCK_HOURS, each block
    is represented by its wettest hour, and the blocks are then combined as if
    independent. That is the usual effective-sample-size argument — a day holds
    roughly 24/tau independent episodes, not 24 — and it puts the answer
    between the two extremes (the 24-hour example lands at 76%).

    Blocking is done on the clock hour, not on position in the list, so it
    behaves the same way on the later forecast days where Met Éireann switches
    from hourly to 6-hourly steps.

    The result is never below the wettest single hour, which is the
    mathematical floor for "rain at some point", and never above 100%.
    """
    blocks = {}
    for row in hourly_rows:
        prob = row.get("rain_prob")
        if prob is None:
            continue
        try:
            hour = int(str(row.get("time_str", "")).split(":")[0])
        except (TypeError, ValueError):
            continue
        q = min(max(prob / 100.0, 0.0), 1.0)
        key = hour // RAIN_BLOCK_HOURS
        blocks[key] = max(blocks.get(key, 0.0), q)

    if not blocks:
        return None

    dry = 1.0
    for q in blocks.values():
        dry *= (1.0 - q)
    return round((1.0 - dry) * 100.0, 1)


def display_day(fc, units=None):
    """Round and convert a daily forecast for display and for the LLM prompt.

    The *_text fields are what gets shown and are in the user's chosen units.
    The bare numbers stay in canonical units (°C, mm, m/s, %) because the
    weather-icon thresholds and other logic are calibrated against those.
    """
    u = units or DEFAULT_SETTINGS
    wind_raw = fc.get("wind_avg")
    rain_raw = fc.get("rain_total")
    prob_raw = fc.get("rain_prob_day")
    return {
        "temp_min": round_temp(fc.get("temp_min")),
        "temp_max": round_temp(fc.get("temp_max")),
        "temp_text": format_temp_range(fc.get("temp_min"), fc.get("temp_max"),
                                       u.get("temp_unit", "c")),
        "rain_total": round_rain(rain_raw),
        "rain_text": format_rain(rain_raw, u.get("rain_unit", "mm")),
        "rain_prob": round_pct(prob_raw),
        "rain_prob_text": format_prob(prob_raw),
        "wind_ms": round_wind_ms(wind_raw),
        "wind_text": format_wind(wind_raw, u.get("wind_units")),
        "humidity": round_pct(fc.get("humidity_avg")),
    }


def display_hour(hr, units=None):
    """Round and convert a single hourly row for display and for the prompt."""
    u = units or DEFAULT_SETTINGS
    wind_raw = hr.get("wind")
    gust_raw = hr.get("wind_gust")
    return {
        "time_str": hr.get("time_str", ""),
        "temperature": round_temp(hr.get("temperature")),
        "temp_text": format_temp(hr.get("temperature"), u.get("temp_unit", "c")),
        "rain_mm": round_rain(hr.get("rain_mm")),
        "rain_text": format_rain(hr.get("rain_mm"), u.get("rain_unit", "mm")),
        "rain_prob": round_pct(hr.get("rain_prob")),
        "rain_prob_text": format_prob(hr.get("rain_prob")),
        "wind_ms": round_wind_ms(wind_raw),
        "wind_text": format_wind(wind_raw, u.get("wind_units")),
        "wind_gust_text": format_wind(gust_raw, u.get("wind_units")),
        "wind_direction": hr.get("wind_direction"),
        "cloudiness": round_pct(hr.get("cloudiness")),
        "humidity": round_pct(hr.get("humidity")),
        "dew_point": round_temp(hr.get("dew_point")),
        "dew_point_text": format_temp(hr.get("dew_point"), u.get("temp_unit", "c")),
        "pressure": None if hr.get("pressure") is None else int(round(hr["pressure"])),
    }


def unit_instructions(prefs):
    """The units paragraph of the prompt, built from the user's choices so the
    prose matches the figures in the forecast data below it."""
    temp = TEMP_LABEL.get(prefs.get("temp_unit", "c"), "°C")
    other_temp = "Fahrenheit" if temp == "°C" else "Celsius"
    names = {"kmh": "km/h", "ms": "m/s", "mph": "mph", "kn": "knots",
             "bft": "the Beaufort scale (written as F5, F6 and so on)"}
    picked = [names[u] for u in WIND_UNITS if u in prefs.get("wind_units", [])] or ["km/h"]
    if len(picked) == 1:
        wind = picked[0]
    else:
        wind = ", ".join(picked[:-1]) + " and " + picked[-1]
    rain = "millimetres (mm)" if prefs.get("rain_unit", "mm") == "mm" else "inches (in)"
    return (
        f'Always give temperatures in {temp} only, e.g. "around 19{temp}". '
        f'Never mention {other_temp}.\n'
        f'Always give wind speed in {wind}, exactly as the data below writes it, '
        f'and in no other unit.\n'
        f'Always give rainfall in {rain}.'
    )


# ─── Met Éireann forecast ─────────────────────────────────────────────────────

# Not cached here: results are cached in _cached_locations (with a TTL), and
# only successful fetches are stored, so a temporary API failure isn't kept.
def fetch_forecast(lat, lon):
    url = f"http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast?lat={lat};long={lon}"
    try:
        resp = requests.get(url, headers={"User-Agent": "MetGPT/1.0"}, timeout=8)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.error("Met Éireann error: %s", e)
        return {}

    product = root.find("product")
    if product is None:
        return {}

    def gattr(node, tag, attr, cast=float):
        child = node.find(tag)
        if child is not None and child.get(attr) not in (None, ""):
            try:
                return cast(child.get(attr))
            except Exception:
                return None
        return None

    hourly = {}
    for tnode in product.findall("time"):
        ts = tnode.get("from")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M")
        loc = tnode.find("location")
        if loc is None:
            continue
        if ts not in hourly:
            hourly[ts] = {"date_str": date_str, "time_str": time_str}
        e = hourly[ts]
        t = gattr(loc, "temperature", "value")
        if t is not None:
            e["temperature"] = round(t, 1)
        pr = loc.find("precipitation")
        if pr is not None:
            try:
                e["rain_mm"] = round(float(pr.get("value", 0)), 1)
                e["rain_prob"] = round(float(pr.get("probability", 0)), 1)
            except Exception:
                pass
        h = gattr(loc, "humidity", "value")
        if h is not None:
            e["humidity"] = round(h, 0)
        w = gattr(loc, "windSpeed", "mps")
        if w is not None:
            e["wind"] = round(w, 1)
        g = gattr(loc, "windGust", "mps")
        if g is not None:
            e["wind_gust"] = round(g, 1)
        wd = loc.find("windDirection")
        if wd is not None:
            e["wind_direction"] = wd.get("name")
        e["pressure"] = gattr(loc, "pressure", "value")
        e["cloudiness"] = gattr(loc, "cloudiness", "percent")
        e["dew_point"] = gattr(loc, "dewpointTemperature", "value")
        rad = loc.find("globalRadiation")
        if rad is not None and rad.get("value") not in (None, ""):
            try:
                e["global_radiation"] = round(float(rad.get("value")), 1)
            except Exception:
                pass

    all_forecasts = {}
    daily_stats = {}
    for row in hourly.values():
        d = row["date_str"]
        stats = daily_stats.setdefault(d, {"temps": [], "rains": [], "hums": [], "winds": []})
        if row.get("temperature") is not None: stats["temps"].append(row["temperature"])
        if row.get("rain_mm") is not None: stats["rains"].append(row["rain_mm"])
        if row.get("humidity") is not None: stats["hums"].append(row["humidity"])
        if row.get("wind") is not None: stats["winds"].append(row["wind"])
        day = all_forecasts.setdefault(d, {"forecast": {"date": d}, "hourly_data": []})
        day["hourly_data"].append(row)

    for d, s in daily_stats.items():
        fc = all_forecasts[d]["forecast"]
        fc.update({
            "date": d,
            "temp_min": round(min(s["temps"]), 1) if s["temps"] else None,
            "temp_max": round(max(s["temps"]), 1) if s["temps"] else None,
            "rain_total": round(sum(s["rains"]), 1) if s["rains"] else None,
            # Chance of rain at some point in the day, not the mean hourly chance.
            "rain_prob_day": daily_rain_probability(all_forecasts[d]["hourly_data"]),
            "humidity_avg": round(sum(s["hums"]) / len(s["hums"]), 0) if s["hums"] else None,
            "wind_avg": round(sum(s["winds"]) / len(s["winds"]), 1) if s["winds"] else None,
        })

    return all_forecasts


# ─── Met Éireann warnings ─────────────────────────────────────────────────────

def fetch_warnings():
    warnings = []
    try:
        d = feedparser.parse("https://alerts.met.ie/Atom/")
        for entry in d.entries:
            effective = expires = None
            if getattr(entry, "published_parsed", None):
                effective = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if getattr(entry, "expires_parsed", None):
                expires = datetime(*entry.expires_parsed[:6], tzinfo=timezone.utc)
            areas = entry.get("met:_area", [])
            warnings.append({
                "title": entry.get("title", ""),
                "description": entry.get("summary", ""),
                "effective": effective,
                "expires": expires,
                "areas": areas,
            })
    except Exception as e:
        logger.error("Warnings fetch error: %s", e)
    return warnings


def warnings_for(warnings, location_name, date_iso):
    results = []
    try:
        req_date = datetime.fromisoformat(date_iso).date()
    except Exception:
        return results
    for w in warnings:
        eff = w["effective"].date() if w["effective"] else None
        exp = w["expires"].date() if w["expires"] else None
        if eff and exp and eff <= req_date <= exp:
            for area in w["areas"]:
                if location_name.lower() in area.lower() or area.lower() in location_name.lower():
                    results.append(w)
                    break
    return results


# ─── Advice generation ────────────────────────────────────────────────────────

# Kept deliberately narrow and literal — matched in code, not left to the LLM's
# judgment, so a profile like "allergies from mice" or "allergic to rice" can't
# get associatively linked to pollen the way a free-text prompt instruction can.
POLLEN_CONDITION_KEYWORDS = (
    "asthma", "hay fever", "hayfever", "pollen", "flower dust",
    "allergic rhinitis", "rhinitis", "spore",
)


def _profile_mentions_pollen_condition(user_ctx):
    ctx_lower = user_ctx.lower()
    return any(kw in ctx_lower for kw in POLLEN_CONDITION_KEYWORDS)


def generate_advice(forecasts, activities, user_text, history, query_type):
    prefs = current_settings()
    user_ctx = prefs["user_context"].strip()
    intro = f"User profile: {user_ctx}\n\n" if user_ctx else ""

    # Every figure here has already been rounded by display_day/display_hour,
    # so the prompt, the forecast cards and the hourly table can never disagree
    # and the model is never shown spurious precision it might echo back.
    lines = []
    for f in forecasts:
        loc = f.get("location", "N/A")
        date = f.get("date", "N/A")
        d = f.get("display", {})
        lines.append(
            f"Location: {loc}, Date: {date} | "
            f"Temp: {d.get('temp_text')} | "
            f"Rain: {d.get('rain_text')} ({d.get('rain_prob_text')} chance) | "
            f"Wind: {d.get('wind_text')} | "
            f"Humidity: {d.get('humidity')}%"
        )
        for hr in f.get("hourly", []):
            parts = []
            if hr.get("temp_text") is not None: parts.append(hr["temp_text"])
            if hr.get("rain_text") is not None: parts.append(f"rain {hr['rain_text']}")
            if hr.get("rain_prob_text") is not None: parts.append(f"{hr['rain_prob_text']} chance")
            if hr.get("wind_text") is not None: parts.append(f"wind {hr['wind_text']}")
            if hr.get("wind_direction"): parts.append(hr["wind_direction"])
            if hr.get("cloudiness") is not None: parts.append(f"cloud {hr['cloudiness']}%")
            if hr.get("dew_point_text") is not None: parts.append(f"dew {hr['dew_point_text']}")
            if parts:
                lines.append(f"  {hr.get('time_str', '')}: {', '.join(parts)}")

    forecast_text = "\n".join(lines) if lines else "No forecast data available."

    unit_note = unit_instructions(prefs)
    today_str = datetime.now().strftime("%A, %d %B %Y")
    multi = len(forecasts) > 1
    range_note = (
        "The user asked about multiple days. Give a short overview sentence, then a brief "
        "day-by-day rundown (one short paragraph per day), then end with overall advice for "
        "their activities across the period.\n"
        if multi else ""
    )

    # Answer-length preference: "short" or "detailed" (default detailed)
    length_pref = prefs["answer_length"]
    if length_pref == "short":
        length_note = ("Keep the answer brief: 2-3 sentences maximum. State the key numbers and "
                       "one practical takeaway. No long explanations.\n")
    else:
        length_note = ("Give a thorough, friendly answer with practical detail on what to wear, "
                       "activity suitability, and any risks.\n")

    pollen_note = (
        "The weather data below does NOT include pollen or hay fever information — Met Éireann's "
        "forecast API has no pollen data. Never invent or estimate a pollen forecast, pollen count, "
        "or pollen type under any circumstance. If — and only if — the user's message above asks "
        "about pollen, hay fever, or allergens directly, tell them you don't have live pollen data "
        "and point them to https://www.met.ie/forecasts/pollen (which also shows which pollen types "
        "are currently in the air). If they didn't ask about pollen, don't bring it up at all — this "
        "applies regardless of anything else in their profile.\n"
    )
    if _profile_mentions_pollen_condition(user_ctx):
        pollen_note += (
            "Their profile above does mention a pollen-related condition (asthma/hay fever/pollen "
            "allergy), so even though this message doesn't ask about pollen, briefly mention as a "
            "relevant aside that they can check https://www.met.ie/forecasts/pollen for the latest "
            "pollen forecast — keep it short, one sentence, don't dwell on it.\n"
        )

    prompt = f"""{intro}You are MetGPT, a helpful Irish weather assistant. Today is {today_str}.
When mentioning any date, calculate the correct day of the week from the date string in the data — do not guess. This query is classified as "{query_type}".
{unit_note}

TONE — this matters:
Write the way an ordinary Irish person writes today, or the way a Met Éireann forecaster talks: plain, warm and matter-of-fact. Never use stage-Irish or tourist-brochure Irishness. Specifically banned: "top o' the morning", "begorrah", "faith and", "sure and", "to be sure", "bejaysus", "arrah", "musha", "the craic" dropped in for colour, leprechauns, shamrocks, and any mock-brogue spelling such as "ye olde" or "t'is". Do not open with a greeting, a salutation or the user's name. Start with the weather itself. Words that genuinely belong to everyday Irish English are fine where they fit naturally, and so are Irish-language words that people actually use, but never reach for either as decoration.

PRECISION — this matters. Every figure below has already been rounded to a step a forecast can actually support, so never state a number more precisely than the data does:
- Copy each figure exactly as it is written, unit included. Never add a decimal place that is not there, and never convert it into a different unit.
- Temperatures are whole degrees. Never write a decimal temperature.
- Probabilities are to the nearest 5%. Never write 0% or 100% — we are never that certain. Use "<5%" or ">95%" as the data does, or wording like "very unlikely" and "almost certain".
- Humidity and cloud cover are to the nearest 5%.
The daily rain chance below is the chance of rain at SOME POINT that day, not the chance in any one hour, so describe it that way. The per-hour figures in the hourly breakdown are the chance in that hour.
If you work out any figure yourself (an average, a range, a maximum), round it the same way before writing it.
Always be polite, friendly, and never rude or offensive to people. Do not swear ever. Do not criticise Met Éireann.
{pollen_note}{length_note}{range_note}
User asked: {user_text}

Weather data:
{forecast_text}

Activities: {', '.join(activities) if activities else 'none specified'}

Advice:"""
    return call_llm_throttled(prompt)


# ─── Core query processor ─────────────────────────────────────────────────────

def process_query(user_text, prev_locs=None, prev_dates=None):
    global _cached_locations, _warnings_cache, _warnings_last_fetched

    history = session.get("chat_history", [])
    responses = []

    # 1) Classify first — handle non-weather / junk immediately
    query_type = classify_query(user_text, history)
    if query_type == "junk":
        responses.append({
            "type": "advice",
            "text": ("I'm MetGPT, an Irish weather assistant, so I can only help with weather "
                     "questions for locations in Ireland. Try asking me something like "
                     "\"Will it rain in Galway tomorrow?\""),
        })
        return responses, prev_locs or [], prev_dates or []

    # 2) Extract candidates
    candidates = extract_locations_dates(user_text, history)

    # Split candidates: a location only counts as "explicit" if the user actually
    # typed it in this message. Any other candidate contributes its DATE only,
    # so dates never get lost when Gemini attaches a context location to them.
    explicit_locs = []
    date_only = []
    seen_dates = set()
    for e in candidates:
        loc = e["location"]
        if loc and loc.lower() in user_text.lower():
            explicit_locs.append(e)
        else:
            if e["date"] not in seen_dates:
                seen_dates.add(e["date"])
                date_only.append({"location": None, "date": e["date"]})

    # First-ever message → always standalone
    if not prev_locs:
        query_type = "standalone"

    # New explicit place different from last → standalone
    if prev_locs and explicit_locs:
        new_set = {e["location"].lower() for e in explicit_locs}
        old_set = {e["location"].lower() for e in prev_locs}
        if new_set != old_set:
            query_type = "standalone"

    multi_day_request = len(date_only) > 1

    # 3) Geocode any explicitly-typed locations
    # A multi-day request (e.g. "this weekend") can produce several candidates
    # that share the same location name, one per date. Geocoding — and any
    # resulting error message — depends only on the name, not the date, so we
    # resolve and report each distinct name once and reuse the result across
    # every date that shares it, instead of geocoding and messaging per-date.
    valid_locs = []
    had_explicit_location = len(explicit_locs) > 0
    geocode_results = {}
    reported_locs = set()
    for e in explicit_locs:
        loc_key = e["location"].lower()
        if loc_key not in geocode_results:
            geocode_results[loc_key] = geocode_location(e["location"])
        lat, lon, _, ambiguous_counties = geocode_results[loc_key]
        if lat is not None:
            valid_locs.append({"location": e["location"], "date": e["date"]})
        elif loc_key in reported_locs:
            continue
        elif ambiguous_counties:
            reported_locs.add(loc_key)
            responses.append({
                "type": "error",
                "text": (f"There's more than one '{e['location']}' in Ireland — I found it in "
                         f"{', '.join(ambiguous_counties)}. Could you say which county you mean? "
                         f"(e.g. \"{e['location']}, {ambiguous_counties[0]}\")"),
            })
            # Remember every date that was requested for this ambiguous name
            # (e.g. all 7 days of "Newcastle for the coming 7 days") so that
            # once the user disambiguates it in a later message ("Newcastle,
            # County Dublin") without repeating the date range, we can restore
            # it instead of silently collapsing to just today.
            pending_dates = sorted({x["date"] for x in explicit_locs if x["location"].lower() == loc_key})
            if len(pending_dates) > 1:
                session["pending_ambiguous_dates"] = {"base": loc_key, "dates": pending_dates}
        else:
            reported_locs.add(loc_key)
            responses.append({
                "type": "error",
                "text": (f"I couldn't find '{e['location']}' in Ireland. I'm an Irish weather "
                         "assistant, so I can only help with locations within Ireland."),
            })

    # If the user explicitly named a place but NONE of them were valid Irish
    # locations, stop here. Don't silently fall back to their home location.
    if had_explicit_location and not valid_locs:
        return responses, prev_locs or [], prev_dates or []

    # 3b) Restore a pending multi-day range once an ambiguous place from an
    # earlier message gets disambiguated (e.g. "Newcastle, County Dublin"
    # replying to the Newcastle prompt above). Only applies when this message
    # resolved to exactly one place and didn't specify its own date range —
    # if it did, that explicit request takes priority instead.
    pending = session.pop("pending_ambiguous_dates", None)
    if pending and len(explicit_locs) == 1 and len(valid_locs) == 1 \
            and pending["base"] in valid_locs[0]["location"].lower():
        resolved_loc = valid_locs[0]["location"]
        valid_locs = [{"location": resolved_loc, "date": d} for d in pending["dates"]]
        responses.append({"type": "info", "text": f"Showing {len(pending['dates'])} days for {resolved_loc}"})

    # 4) Follow-up reuse — only when NOT a multi-day range request
    if not valid_locs and query_type == "followup" and prev_locs and not multi_day_request:
        unique_locs = []
        seen_loc = set()
        for v in prev_locs:
            if v["location"].lower() not in seen_loc:
                seen_loc.add(v["location"].lower())
                unique_locs.append(v["location"])

        if date_only:
            new_date = date_only[0]["date"]
            valid_locs = [{"location": loc, "date": new_date} for loc in unique_locs]
        else:
            valid_locs = [dict(v) for v in prev_locs]

        responses.append({"type": "info", "text": "Using your previous location: " + ", ".join(unique_locs)})

    # 5) Multi-day range with a known previous location → expand across all dates
    if not valid_locs and multi_day_request and prev_locs:
        base_loc = prev_locs[0]["location"]
        valid_locs = [{"location": base_loc, "date": d["date"]} for d in date_only]
        responses.append({"type": "info", "text": f"Showing {len(date_only)} days for {base_loc}"})

    # 6) Still nothing → home location, then ask the user to provide one
    if not valid_locs:
        home = current_settings()["home_location"]
        if home:
            today = datetime.now().strftime("%Y-%m-%d")
            dates = [d["date"] for d in date_only] if date_only else [today]
            valid_locs = [{"location": home, "date": d} for d in dates]
            if len(dates) > 1:
                responses.append({"type": "info", "text": f"Showing {len(dates)} days for {home}"})
            else:
                responses.append({"type": "info", "text": f"No location mentioned — using your home location: {home}"})
        else:
            responses.append({
                "type": "error",
                "text": ("Please enter an Irish location — either type one in your question "
                         "(e.g. \"weather in Galway tomorrow\") or set a home location on the left."),
            })
            return responses, prev_locs or [], prev_dates or []

    activities = extract_activities(user_text, history)

    now = datetime.now()
    if not _warnings_cache or (
        _warnings_last_fetched and (now - _warnings_last_fetched).total_seconds() > 3600
    ):
        _warnings_cache = fetch_warnings()
        _warnings_last_fetched = now

    is_multi = len(valid_locs) > 1

    all_for_advice = []
    for entry in valid_locs:
        loc_name, date_iso = entry["location"], entry["date"]

        prefs = current_settings()
        home = prefs["home_location"]
        if home and loc_name == home and prefs["home_lat"] is not None:
            lat = prefs["home_lat"]
            lon = prefs["home_lon"]
            display_name = prefs["home_display"] or loc_name
        else:
            lat, lon, display_name, ambiguous_counties = geocode_location(loc_name)

        if lat is None:
            if ambiguous_counties:
                responses.append({
                    "type": "error",
                    "text": (f"There's more than one '{loc_name}' in Ireland — I found it in "
                             f"{', '.join(ambiguous_counties)}. Could you say which county you mean?"),
                })
            else:
                responses.append({"type": "error", "text": f"I couldn't find '{loc_name}' in Ireland."})
            continue

        for w in warnings_for(_warnings_cache, display_name, date_iso):
            responses.append({"type": "warning", "text": f"Weather warning for {display_name} on {date_iso}: {w['title']} — {w['description']}"})

        key = (round(lat, 4), round(lon, 4))
        forecasts = _cached_locations.get(key)
        if not forecasts:
            forecasts = fetch_forecast(lat, lon)
            if not forecasts:
                responses.append({"type": "error", "text": f"Could not fetch forecast for {display_name}."})
                continue
            _cached_locations[key] = forecasts

        if date_iso in forecasts:
            day = forecasts[date_iso]
            fc = day["forecast"]
            # Round once, here, and reuse for both the forecast card and the
            # advice prompt so the two can never show different numbers.
            units = current_settings()
            shown = display_day(fc, units)
            hours = [
                display_hour(h, units)
                for h in sorted(day["hourly_data"], key=lambda x: x.get("time_str", ""))
            ]
            all_for_advice.append({
                "location": display_name,
                "date": date_iso,
                "display": shown,
                "hourly": hours,
            })
            responses.append({
                "type": "forecast",
                "location": display_name,
                "date": date_iso,
                "compact": is_multi,
                "hourly": hours,
                **shown,
            })
        else:
            responses.append({"type": "error", "text": f"No forecast available for {display_name} on {date_iso}. Met Éireann only forecasts about 9 days ahead."})

    if all_for_advice:
        advice = generate_advice(all_for_advice, activities, user_text, history, query_type)
        responses.append({"type": "advice", "text": advice})
    elif not any(r["type"] == "error" for r in responses):
        responses.append({"type": "advice", "text": "I couldn't find forecast data for that query. Please try a different location or date."})

    new_dates = [e["date"] for e in valid_locs]
    return responses, valid_locs, new_dates


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in_user"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if check_credentials(username, password):
            session["logged_in_user"] = username
            session.modified = True
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    if "sid" not in session:
        session["sid"] = secrets.token_urlsafe(16)
    if "chat_history" not in session:
        session["chat_history"] = []
    prefs = current_settings()
    return render_template("index.html",
        settings=prefs,
        home_location=prefs["home_location"],
        home_display=prefs["home_display"] or prefs["home_location"],
        user_context=prefs["user_context"],
        username=session.get("logged_in_user", ""),
        answer_length=prefs["answer_length"],
    )


@app.route("/chat", methods=["POST"])
@login_required
@rate_limit
def chat():
    data = request.get_json()
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "Empty message"}), 400

    if "chat_history" not in session:
        session["chat_history"] = []
    if "sid" not in session:
        session["sid"] = secrets.token_urlsafe(16)

    session["chat_history"].append({"role": "user", "content": user_text})

    prev_locs = session.get("last_valid_locs")
    prev_dates = session.get("last_dates")

    responses, new_locs, new_dates = process_query(user_text, prev_locs, prev_dates)

    advice_parts = []
    for r in responses:
        if r["type"] == "advice":
            advice_parts.append(r["text"])
    combined_advice = " ".join(advice_parts)
    if combined_advice:
        session["chat_history"].append({"role": "assistant", "content": combined_advice})

    # Trim history so it fits within cookie-session size limits on Railway.
    max_history = 10 if USE_COOKIE_SESSIONS else 40
    if len(session["chat_history"]) > max_history:
        session["chat_history"] = session["chat_history"][-max_history:]

    session["last_valid_locs"] = new_locs
    session["last_dates"] = new_dates
    session.modified = True

    return jsonify({"responses": responses})


@app.route("/settings", methods=["POST"])
@login_required
@rate_limit
def settings():
    data = request.get_json()
    changes = {}

    # Only touch the fields this request actually sent, so the location
    # popover and the preferences panel can save independently of each other.
    if "user_context" in data:
        changes["user_context"] = (data.get("user_context") or "").strip()
    answer_length = (data.get("answer_length") or "").strip().lower()
    if answer_length in ("short", "detailed"):
        changes["answer_length"] = answer_length
    temp_unit = (data.get("temp_unit") or "").strip().lower()
    if temp_unit in TEMP_UNITS:
        changes["temp_unit"] = temp_unit
    rain_unit = (data.get("rain_unit") or "").strip().lower()
    if rain_unit in RAIN_UNITS:
        changes["rain_unit"] = rain_unit
    if isinstance(data.get("wind_units"), list):
        picked = [u for u in WIND_UNITS if u in data["wind_units"]]
        if picked:
            changes["wind_units"] = picked

    home = (data.get("home_location") or "").strip()
    if home:
        lat, lon, display, ambiguous_counties = geocode_location(home)
        if lat:
            changes.update({"home_location": home, "home_lat": lat,
                            "home_lon": lon, "home_display": display})
        elif ambiguous_counties:
            return jsonify({"error": (f"There's more than one '{home}' in Ireland — found in "
                                       f"{', '.join(ambiguous_counties)}. Try adding the county, "
                                       f"e.g. \"{home}, {ambiguous_counties[0]}\".")}), 400
        else:
            return jsonify({"error": f"Could not find '{home}' in Ireland. Try a different spelling."}), 400

    prefs = update_settings(**changes)
    return jsonify({"ok": True,
                    "home_display": prefs["home_display"] or prefs["home_location"],
                    "has_home": bool(prefs["home_location"])})


@app.route("/geolocate", methods=["POST"])
@login_required
@rate_limit
def geolocate():
    data = request.get_json()
    lat = data.get("lat")
    lon = data.get("lon")
    if lat is None or lon is None:
        return jsonify({"error": "No coordinates provided."}), 400

    place, country = reverse_geocode(lat, lon)
    if not place:
        return jsonify({"error": "Could not determine your location."}), 400

    if country and country.lower() != "ie":
        return jsonify({"error": "Your detected location is outside Ireland. MetGPT only covers Irish weather."}), 400

    update_settings(home_location=place, home_lat=float(lat),
                    home_lon=float(lon), home_display=place)
    return jsonify({"ok": True, "home": place, "has_home": True})


@app.route("/clear", methods=["POST"])
@login_required
def clear():
    session.pop("chat_history", None)
    session.pop("last_valid_locs", None)
    session.pop("last_dates", None)
    session.modified = True
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    # debug is on locally; Railway runs via gunicorn (see Procfile) so this
    # block only executes for local development.
    app.run(host="0.0.0.0", port=port, debug=True)