# MetGPT — AI-Powered Irish Weather Assistant

MetGPT is a conversational weather assistant for Ireland. It combines Google's **Gemini 2.5 Flash Lite** language model with **official Met Éireann forecast data** to answer natural-language weather questions. Every number MetGPT gives comes directly from Met Éireann; the LLM only interprets and explains that real data.

---

## Key Features

- **Natural-language weather chat** — ask questions like *"Will it rain in Galway this weekend?"* or *"What should I wear for a run in Cork tomorrow?"*
- **Grounded in real data** — all forecasts come from the Met Éireann Open API; the LLM never invents weather
- **Multi-day support** — ask about a single day, "this weekend", or "the coming week" and get an appropriately formatted answer
- **Conversational memory** — follow-up questions like *"and for tomorrow?"* work naturally within a session
- **Live weather warnings** — active Met Éireann alerts are surfaced for the relevant location and date
- **Personalised advice** — set a home location, tell it about yourself (e.g. "I cycle to work and have asthma"), and choose short or detailed answers
- **GPS location** — a "Use my location" button detects the user's position via the browser and reverse-geocodes it
- **Ireland-scoped** — politely refuses non-weather questions and locations outside Ireland
- **User authentication** — username/password login protects the LLM from public abuse when deployed

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend / web framework | **Flask** (Python) |
| Language model | **Gemini 2.5 Flash Lite**  |
| Weather data | **Met Éireann Open API**  |
| Geocoding | **LocationIQ**  |
| Frontend | HTML, CSS, JavaScript |
| Session storage | Flask-Session (filesystem) |

---

## Architecture

MetGPT is not an autonomous tool-using agent. It is a **structured LLM pipeline** where the application code controls the sequence of steps, and Gemini performs specific natural-language sub-tasks within that flow. This deterministic design guarantees that weather data always comes from Met Éireann and makes the system predictable and easy to debug.

**Flow for a single query:**

```
User message
    │
    ▼
classify_query()          → followup / standalone / junk   (Gemini)
    │
    ▼
extract_locations_dates() → which places & dates?          (Gemini)
    │
    ▼
geocode_location()        → place name → coordinates        (LocationIQ)
    │
    ▼
fetch_forecast()          → coordinates → hourly + daily    (Met Éireann)
fetch_warnings()          → active alerts                   (Met Éireann RSS)
    │
    ▼
generate_advice()         → interpret real data → advice    (Gemini)
    │
    ▼
JSON response
```

The orchestration lives in `process_query()` in `app.py`.

---

## Setup

### 1. Prerequisites

- Python 3.10+
- A **Gemini API key** — https://aistudio.google.com/app/apikey
- A **LocationIQ API key** — https://locationiq.com (free tier available)

### 2. Clone and create a virtual environment

```bash
cd metgpt
python3 -m venv venv
source venv/bin/activate        
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create a `.env` file

In the `metgpt/` folder, create a file called `.env`:

```
GEMINI_API_KEY=your_gemini_api_key_here
LOCATIONIQ_KEY=your_locationiq_key_here
FLASK_SECRET_KEY=any_long_random_string_here
METGPT_USERS=user1:yourpassword,user2:anotherpassword
```

- **`GEMINI_API_KEY`** — your Gemini key
- **`LOCATIONIQ_KEY`** — your LocationIQ key
- **`FLASK_SECRET_KEY`** — a random string used to sign session cookies. Generate one with:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- **`METGPT_USERS`** — login accounts as `username:password` pairs, comma-separated. Add as many as you like.

### 5. Run

```bash
python app.py
```

Open **http://localhost:5000** and sign in with one of your accounts.

---

## Usage

1. **Sign in** with a username/password from your `.env`
2. **Set a home location** — click "Use my location" (GPS) so questions without a place default to where you are
3. **Tell MetGPT about yourself** (optional) — e.g. "I cycle to work and have asthma"
4. **Choose answer length** — Short (quick & precise) or Long (detailed advice)
5. **Ask away:**
   - *"Will it rain in Galway this weekend?"*
   - *"Is it a good day for hiking in Wicklow?"*
   - *"weather this week"* → a 7-day summary
   - *"and for tomorrow?"* → follows up on the previous location

---

## How Anti-Hallucination Works

Large language models often invent plausible-sounding but false weather figures. MetGPT prevents this by design:

1. The LLM is **never asked to produce weather numbers**. It only extracts intent (locations, dates, activities) and writes advice.
2. All actual forecast values are fetched by the application from **Met Éireann** and passed to the LLM as ground-truth context.
3. If a location can't be resolved within Ireland, or a date is outside Met Éireann's ~9-day range, MetGPT says so plainly rather than guessing.

This separation — LLM for language, official API for facts — is the core design principle of the project.

---

## Deployment Notes

When deploying (e.g. Railway, Render):

- Set `GEMINI_API_KEY`, `LOCATIONIQ_KEY`, `FLASK_SECRET_KEY`, and `METGPT_USERS` as **environment variables** in the host's dashboard — do **not** commit `.env`.
- Add `.env` to `.gitignore`.
- The "Use my location" GPS feature requires **HTTPS** (or localhost). Hosted platforms provide HTTPS by default.

---
