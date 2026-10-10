# Budlance — Reverse-Budget AI Travel Agent

> **The user's budget is the primary constraint.**
>
> Budlance is a Telegram-based AI travel agent. Give it your budget, number of travelers,
> trip duration, and interests. Budlance discovers travel options via SerpApi, calculates
> whether a complete trip fits your budget, optimizes if needed, generates a day-by-day
> itinerary, tracks the budget in a Virtual Ledger, and supports live Rescue/Replanning
> if the trip hits disruptions (weather, closures, fare disputes).

---

## Table of Contents

1. [Architecture overview](#architecture-overview)
2. [Tech stack](#tech-stack)
3. [Prerequisites](#prerequisites)
4. [Local setup](#local-setup)
5. [Environment variables](#environment-variables)
6. [Running tests](#running-tests)
7. [Starting the application](#starting-the-application)
8. [Verifying the health endpoint](#verifying-the-health-endpoint)
9. [Telegram webhook configuration](#telegram-webhook-configuration)
10. [External API requirements](#external-api-requirements)
11. [Running with Docker](#running-with-docker)
12. [Project structure](#project-structure)

---

## Architecture overview

```
User (Telegram message)
  └─► FastAPI /webhook
        └─► AI Intent Layer (OpenRouter)
              └─► Budlance Orchestrator
                    └─► Cache / Fallback Manager
                          └─► SerpApi Gateway (if no cache/fallback)
                                └─► Data Normalizer
                                      └─► Estimation Layer
                                            └─► Reverse-Budget Engine
                                                  ├─► FEASIBLE
                                                  │     ├─► Itinerary Generator
                                                  │     ├─► Virtual Ledger
                                                  │     ├─► Supabase PostgreSQL
                                                  │     └─► Telegram response
                                                  └─► NOT FEASIBLE
                                                        └─► Optimization Engine (max 4 attempts)
                                                              └─► Loop back to Cache/SerpApi
```

**Rescue flow** (during an active trip):

```
User: "It's raining" / "Auto asking ₹500"
  └─► Load active trip + itinerary + ledger from Supabase
        └─► AI Rescue Intent
              ├─► Weather / Closure: Maps search → alternative → mini feasibility → update itinerary + ledger
              └─► Price Dispute: local rate tables → fare guidance → optional ledger entry
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full layer-by-layer description.

---

## Tech stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12+ (tested on 3.13.1) |
| Package manager | `uv` |
| Web framework | FastAPI + uvicorn |
| Telegram | `python-telegram-bot` v21+ |
| AI gateway | OpenRouter |
| Schema validation | Pydantic v2 |
| HTTP client | httpx |
| Live travel data | SerpApi |
| Database | Supabase (PostgreSQL) |
| Testing | pytest + pytest-asyncio |
| Containerisation | Docker |

---

## Prerequisites

- Python 3.12 or 3.13
- [uv](https://docs.astral.sh/uv/) package manager
- Git

Install `uv` (if not already installed):

```bash
# macOS / Linux
curl -Ls https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
```

---

## Local setup

```bash
# 1. Clone the repository
git clone https://github.com/<your-org>/budlance.git
cd budlance

# 2. Sync the virtual environment from the lockfile
uv sync

# 3. Create your .env file from the template
cp .env.example .env
# Edit .env and fill in your credentials (see Environment variables below)
```

---

## Environment variables

Copy `.env.example` to `.env` and fill in the values below.  
**Never commit `.env` — it is listed in `.gitignore`.**

```env
# ── Required for live Telegram operation ──────────────────────────────────────
TELEGRAM_BOT_TOKEN="<your-bot-token-from-@BotFather>"

# ── Required for AI intent parsing ────────────────────────────────────────────
OPENROUTER_API_KEY="<your-openrouter-key>"
OPENROUTER_MODEL="anthropic/claude-3.5-sonnet"   # change to any supported model

# ── Required for live travel data ─────────────────────────────────────────────
SERPAPI_API_KEY="<your-serpapi-key>"

# ── Required for cross-conversation persistence ───────────────────────────────
SUPABASE_URL="https://<project-id>.supabase.co"
SUPABASE_KEY="<anon-or-service-role-key>"
DATABASE_URL="postgresql://postgres:<password>@db.<project-id>.supabase.co:5432/postgres"

# ── Application settings ──────────────────────────────────────────────────────
APP_ENV="development"        # or "production"
LOG_LEVEL="INFO"
PORT="8000"

# ── Telegram webhook (required for production; leave empty for polling/local) ──
WEBHOOK_URL=""               # e.g. https://your-domain.com
```

### Which variables are required vs. optional?

| Variable | Required for… | Fallback behaviour |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Live Telegram delivery | App runs in degraded/offline mode |
| `OPENROUTER_API_KEY` | AI intent parsing | Requests fail at AI layer |
| `SERPAPI_API_KEY` | Live flight/hotel/map data | Static fallback data is used |
| `SUPABASE_URL` + `SUPABASE_KEY` | Cross-session persistence | In-memory stores (no durability) |
| `DATABASE_URL` | Direct PostgreSQL access | Supabase client used instead |
| `WEBHOOK_URL` | Webhook-mode Telegram | Leave empty for local/test use |

---

## Running tests

```bash
# Run the complete test suite (119 tests)
uv run pytest tests/ -v

# Run a specific test file
uv run pytest tests/test_orchestrator_and_integration.py -v

# Run a single test
uv run pytest tests/test_phase12_hardening.py::test_H8_second_trip_deactivates_first -v

# Validate the application can be imported without errors
uv run python -c "from budlance.api.app import app; print('Import OK')"
```

---

## Starting the application

```bash
# Development (with auto-reload)
uv run uvicorn budlance.api.app:app --host 0.0.0.0 --port 8000 --reload

# Production
uv run uvicorn budlance.api.app:app --host 0.0.0.0 --port 8000
```

The ASGI entry point is `budlance.api.app:app`.

---

## Verifying the health endpoint

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{
  "status": "healthy",
  "app_env": "development",
  "version": "0.1.0",
  "telegram_configured": true
}
```

`telegram_configured: false` means `TELEGRAM_BOT_TOKEN` is empty — the rest of the
application still works (tests pass, intent/budget/ledger/rescue logic all functions).

---

## Telegram webhook configuration

Budlance receives Telegram messages through a **webhook** at the `/webhook` endpoint.

**Requirements:**

1. A publicly reachable HTTPS URL (e.g. via [ngrok](https://ngrok.com/) for local dev or a cloud deployment).
2. The `WEBHOOK_URL` environment variable set to your base URL.
3. Register the webhook with Telegram:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://your-domain.com/webhook"
```

**Local development with ngrok:**

```bash
# In terminal 1 — start the app
uv run uvicorn budlance.api.app:app --port 8000

# In terminal 2 — expose with ngrok
ngrok http 8000

# Register the ngrok HTTPS URL as webhook
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<ngrok-id>.ngrok.io/webhook"
```

**Note:** Without a configured webhook, Telegram will not deliver messages to the application.
All business logic (budget engine, normalizer, ledger, rescue) can still be exercised via
the test suite without any Telegram connection.

---

## External API requirements

| Service | Purpose | Fallback |
|---|---|---|
| **Telegram Bot API** | User interface | Tests work without it |
| **OpenRouter** | Intent parsing (LLM) | No fallback; AI layer will error |
| **SerpApi** | Live flights, hotels, maps, directions | Static fallback JSON for trains/buses; fallback catalog for destinations |
| **Supabase PostgreSQL** | Persistence, cross-session state | In-memory store (no durability between restarts) |

SerpApi engines used by Budlance:
- `google_travel_explore` — destination discovery
- `google_flights` — flight pricing
- `google_hotels` — hotel options and pricing
- `google_maps` — local attractions and places
- `google_maps_directions` — routes and travel time

See [`docs/SERPAPI_USAGE.md`](docs/SERPAPI_USAGE.md) for full details.

---

## Running with Docker

```bash
# Build the image
docker build -t budlance .

# Run with environment variables injected from .env
docker run --env-file .env -p 8000:8000 budlance

# Or use docker-compose (mounts .env automatically)
docker compose up
```

Health check:

```bash
curl http://localhost:8000/health
```

---

## Project structure

```
budlance/
├── src/budlance/
│   ├── api/          # FastAPI app, routes, health endpoint, /webhook
│   ├── bot/          # Telegram bot setup and message handlers
│   ├── ai/           # OpenRouter client and intent/rescue parsing
│   ├── orchestrator/ # BudlanceOrchestrator — central coordinator
│   ├── cache/        # CacheFallbackManager — Cache → Fallback → SerpApi
│   ├── serpapi/      # SerpApi gateway, rate limiter, retry/backoff
│   ├── normalization/# DataNormalizer — SerpApi JSON → internal models
│   ├── estimation/   # EstimationLayer — food, local transit heuristics
│   ├── engine/       # ReverseBudgetEngine + OptimizationEngine
│   ├── itinerary/    # ItineraryGenerator
│   ├── ledger/       # VirtualLedgerManager
│   ├── rescue/       # RescueService — weather/closure/price dispatch
│   ├── db/           # Supabase client, domain models, repositories
│   ├── schemas/      # Shared travel schemas (FlightOption, HotelOption, …)
│   └── config.py     # Centralized pydantic-settings configuration
├── tests/            # 746 pytest tests (100% offline baseline)
├── data/             # Static fallback datasets (trains, buses, rate tables)
├── docs/
│   ├── DEMO_SCRIPT.md    # Rigid, rehearsal-verified 5-scenario live demo script
│   ├── PROJECT_SPEC.md   # Full architecture specification
│   ├── ARCHITECTURE.md   # Developer architecture guide
│   └── SERPAPI_USAGE.md  # SerpApi integration details
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
├── .gitignore
├── pyproject.toml
├── uv.lock
└── README.md
```

---

## 🎯 Demo Limits & Honest Boundaries (Pitch Guidelines)

- **Provider Direct Payments**: Budlance plans and hands off; travelers pay airlines, hotels, and rail operators directly via authoritative deep links.
- **Train Distance Heuristics**: Indian Railways trains use standard fare and distance rate tables because IRCTC does not offer a public booking API.
- **Payment Sandbox**: Payments run on Stripe test rails (`cs_test_...`) and immediate pass bypass (`/demo_pass`).
- **Prototype Scope**: Budlance is a production-grade hackathon prototype designed for reverse-budget constraint planning.

---

## 🚀 Product Roadmap (Next Phase)

1. **Latent Space & Representation Learning**:
   - Replace rigid keyword interest tagging with dense vector embeddings and **cosine similarity in latent space** (e.g. mapping nuanced user desires like *"quiet misty pine forests"* to hidden hill stations and attractions).
2. **Razorpay & UPI Intent**:
   - Instant UPI payment flows via Telegram Web App and dynamic QR codes for seamless Indian traveler checkout.
3. **Official IRCTC Agent Integration**:
   - Direct Indian Railways PNR availability queries and instant tatkal seat reservation handoffs.
4. **Seasonal Price Forecasting**:
   - Machine-learned demand curves predicting off-season budget windows across 50+ domestic corridors.
