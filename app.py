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

import os, json, re, logging, secrets, threading, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from functools import lru_cache, wraps
from time import time

import requests
import feedparser
from flask import Flask, render_template, request, session, redirect, url_for, abort, jsonify
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
GEMINI_MODEL = "gemini-2.5-flash-lite"

app = Flask(__name__)
# Use a stable secret from .env so logins survive server restarts.
# Falls back to a random key if not set (sessions reset on restart).
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(32)
# Cookie settings — needed for sessions to work over HTTPS (e.g. on Railway)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Only require HTTPS-only cookies in production (Railway sets RAILWAY_ENVIRONMENT)
app.config["SESSION_COOKIE_SECURE"] = os.getenv("RAILWAY_ENVIRONMENT") is not None

# Session backend: use Flask's built-in signed-cookie sessions in production
# (works across gunicorn workers with no shared filesystem). Locally we can
# use filesystem sessions for larger chat histories.
USE_COOKIE_SESSIONS = os.getenv("RAILWAY_ENVIRONMENT") is not None
if not USE_COOKIE_SESSIONS:
    app.config["SESSION_TYPE"] = "filesystem"
    SESSION_DIR = "/tmp/flask_session_metgpt"
    os.makedirs(SESSION_DIR, exist_ok=True)
    app.config["SESSION_FILE_DIR"] = SESSION_DIR
    app.config["SESSION_PERMANENT"] = False
    Session(app)
# else: default Flask cookie sessions (no Session(app) call needed)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

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
_cached_locations = {}
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
  - "location": the place name ONLY if it literally appears in the current user message; otherwise null. NEVER infer or copy a location from the history.
  - "date": "YYYY-MM-DD" or null

Date rules (apply to the CURRENT message only):
- "this week" / "coming week" / "next 7 days" / "the week" -> one object per day for the next 7 days
- "this weekend" / "weekend" -> the upcoming Saturday and Sunday
- "next few days" -> the next 3 days
- "tomorrow" -> tomorrow's date only
- "today" / "now" / "right now" -> today's date only
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
    url = "https://us1.locationiq.com/v1/search.php"
    params = {
        "key": LOCATIONIQ_KEY,
        "q": f"{name}, Ireland",
        "format": "json",
        "countrycodes": "ie",
        "addressdetails": 1,
        "limit": 5,
    }

    # Only accept real settlements / administrative areas — not businesses,
    # shops, restaurants, etc. This stops "delhi" matching "Delhi Rasoi".
    ALLOWED_CLASSES = {"place", "boundary"}
    ALLOWED_TYPES = {
        "city", "town", "village", "hamlet", "suburb", "neighbourhood",
        "locality", "municipality", "county", "state", "administrative",
        "island", "townland",
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        results = resp.json()
        if not isinstance(results, list):
            return None, None, name

        for r in results:
            r_class = (r.get("class") or "").lower()
            r_type = (r.get("type") or "").lower()
            # Accept only genuine settlements / admin areas
            if r_class in ALLOWED_CLASSES or r_type in ALLOWED_TYPES:
                lat = float(r["lat"])
                lon = float(r["lon"])
                # Prefer a clean settlement name from address details if present
                addr = r.get("address", {})
                clean = (addr.get("city") or addr.get("town") or addr.get("village")
                         or addr.get("hamlet") or addr.get("suburb") or addr.get("county"))
                display = clean or r.get("display_name", name)
                logger.info("Geocoded '%s' -> %s (%s/%s)", name, display, r_class, r_type)
                return lat, lon, display

        # Nothing acceptable found (e.g. only a business matched)
        logger.info("Geocode rejected '%s' — no settlement match in Ireland", name)
    except Exception as e:
        logger.error("LocationIQ error for '%s': %s", name, e)
    return None, None, name


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


# ─── Met Éireann forecast ─────────────────────────────────────────────────────

@lru_cache(maxsize=128)
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
        stats = daily_stats.setdefault(d, {"temps": [], "rains": [], "probs": [], "hums": [], "winds": []})
        if row.get("temperature") is not None: stats["temps"].append(row["temperature"])
        if row.get("rain_mm") is not None: stats["rains"].append(row["rain_mm"])
        if row.get("rain_prob") is not None: stats["probs"].append(row["rain_prob"])
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
            "rain_prob_avg": round(sum(s["probs"]) / len(s["probs"]), 1) if s["probs"] else None,
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

def generate_advice(forecasts, activities, user_text, history, query_type):
    user_ctx = session.get("user_context", "").strip()
    intro = f"User profile: {user_ctx}\n\n" if user_ctx else ""

    lines = []
    for f in forecasts:
        loc = f.get("location", "N/A")
        date = f.get("date", "N/A")
        fc = f.get("forecast", {})
        lines.append(
            f"Location: {loc}, Date: {date} | "
            f"Temp: {fc.get('temp_min')}–{fc.get('temp_max')}°C | "
            f"Rain: {fc.get('rain_total')}mm ({fc.get('rain_prob_avg')}% chance) | "
            f"Wind: {fc.get('wind_avg')} m/s | Humidity: {fc.get('humidity_avg')}%"
        )
        for hr in sorted(f.get("hourly_data", []), key=lambda x: x.get("time_str", "")):
            parts = []
            if hr.get("temperature") is not None: parts.append(f"{hr['temperature']}°C")
            if hr.get("rain_mm") is not None: parts.append(f"rain {hr['rain_mm']}mm")
            if hr.get("rain_prob") is not None: parts.append(f"{hr['rain_prob']}% chance")
            if hr.get("wind") is not None: parts.append(f"wind {hr['wind']}m/s")
            if hr.get("wind_direction"): parts.append(hr["wind_direction"])
            if hr.get("cloudiness") is not None: parts.append(f"cloud {hr['cloudiness']}%")
            if hr.get("dew_point") is not None: parts.append(f"dew {hr['dew_point']}°C")
            if parts:
                lines.append(f"  {hr.get('time_str', '')}: {', '.join(parts)}")

    forecast_text = "\n".join(lines) if lines else "No forecast data available."

    today_str = datetime.now().strftime("%A, %d %B %Y")
    multi = len(forecasts) > 1
    range_note = (
        "The user asked about multiple days. Give a short overview sentence, then a brief "
        "day-by-day rundown (one short paragraph per day), then end with overall advice for "
        "their activities across the period.\n"
        if multi else ""
    )

    # Answer-length preference: "short" or "detailed" (default detailed)
    length_pref = session.get("answer_length", "detailed")
    if length_pref == "short":
        length_note = ("Keep the answer brief: 2-3 sentences maximum. State the key numbers and "
                       "one practical takeaway. No long explanations.\n")
    else:
        length_note = ("Give a thorough, friendly answer with practical detail on what to wear, "
                       "activity suitability, and any risks.\n")

    prompt = f"""{intro}You are MetGPT, a helpful Irish weather assistant. Today is {today_str}.
When mentioning any date, calculate the correct day of the week from the date string in the data — do not guess. This query is classified as "{query_type}".
{length_note}{range_note}
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
    valid_locs = []
    had_explicit_location = len(explicit_locs) > 0
    for e in explicit_locs:
        lat, lon, _ = geocode_location(e["location"])
        if lat is not None:
            valid_locs.append({"location": e["location"], "date": e["date"]})
        else:
            responses.append({
                "type": "error",
                "text": (f"I couldn't find '{e['location']}' in Ireland. I'm an Irish weather "
                         "assistant, so I can only help with locations within Ireland."),
            })

    # If the user explicitly named a place but NONE of them were valid Irish
    # locations, stop here. Don't silently fall back to their home location.
    if had_explicit_location and not valid_locs:
        return responses, prev_locs or [], prev_dates or []

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

    # 6) Still nothing → home location, then IP fallback
    if not valid_locs:
        home = session.get("user_home_location")
        if home:
            today = datetime.now().strftime("%Y-%m-%d")
            dates = [d["date"] for d in date_only] if date_only else [today]
            valid_locs = [{"location": home, "date": d} for d in dates]
            if len(dates) > 1:
                responses.append({"type": "info", "text": f"Showing {len(dates)} days for {home}"})
            else:
                responses.append({"type": "info", "text": f"No location mentioned — using your home location: {home}"})
        else:
            ip_lat, ip_lon, ip_name = get_ip_location()
            if ip_lat:
                today = datetime.now().strftime("%Y-%m-%d")
                dates = [d["date"] for d in date_only] if date_only else [today]
                valid_locs = [{"location": ip_name, "date": d} for d in dates]
                responses.append({"type": "info", "text": f"No location mentioned — using your detected location: {ip_name}"})
            else:
                responses.append({"type": "error", "text": "Please mention a location in Ireland so I can check the forecast for you."})
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

        home = session.get("user_home_location")
        if home and loc_name == home and session.get("user_home_lat"):
            lat = session["user_home_lat"]
            lon = session["user_home_lon"]
            display_name = session.get("user_home_display", loc_name)
        else:
            lat, lon, display_name = geocode_location(loc_name)

        if lat is None:
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
            all_for_advice.append({
                "location": display_name,
                "date": date_iso,
                "forecast": fc,
                "hourly_data": day["hourly_data"],
            })
            responses.append({
                "type": "forecast",
                "location": display_name,
                "date": date_iso,
                "temp_min": fc.get("temp_min"),
                "temp_max": fc.get("temp_max"),
                "rain_total": fc.get("rain_total"),
                "rain_prob": fc.get("rain_prob_avg"),
                "wind_avg": fc.get("wind_avg"),
                "humidity": fc.get("humidity_avg"),
                "compact": is_multi,
                "hourly": sorted(day["hourly_data"], key=lambda x: x.get("time_str", "")),
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
    return render_template("index.html",
        home_location=session.get("user_home_location", ""),
        user_context=session.get("user_context", ""),
        username=session.get("logged_in_user", ""),
        answer_length=session.get("answer_length", "detailed"),
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
    home = (data.get("home_location") or "").strip()
    ctx = (data.get("user_context") or "").strip()
    answer_length = (data.get("answer_length") or "").strip().lower()
    if answer_length in ("short", "detailed"):
        session["answer_length"] = answer_length

    if home:
        lat, lon, display = geocode_location(home)
        if lat:
            session["user_home_location"] = home
            session["user_home_lat"] = lat
            session["user_home_lon"] = lon
            session["user_home_display"] = display
        else:
            return jsonify({"error": f"Could not find '{home}' in Ireland. Try a different spelling."}), 400

    session["user_context"] = ctx
    session.modified = True
    return jsonify({"ok": True, "home_display": session.get("user_home_display", home)})


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

    session["user_home_location"] = place
    session["user_home_lat"] = float(lat)
    session["user_home_lon"] = float(lon)
    session["user_home_display"] = place
    session.modified = True
    return jsonify({"ok": True, "home": place})


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