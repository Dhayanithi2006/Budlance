# Budlance — Locked Technical Stack & Architecture

## 1. System Overview
**Budlance** is an AI-powered, reverse-budget travel planning assistant delivered via Telegram. Users declare their total budget, travel party, and constraints, and Budlance optimizes itineraries through dynamic SerpApi data, fallback rate heuristics, and a virtual expense ledger.

---

## 2. Locked Component Matrix

| Layer | Technology | Purpose & Implementation Details |
| :--- | :--- | :--- |
| **User Interface** | Telegram Bot API | Primary user interaction interface |
| **Bot Framework** | `python-telegram-bot` (v20+ async) | Command routing, inline keyboards, conversational state |
| **Web & Orchestration** | `FastAPI` + `uvicorn` | Webhook ingestion, API routes, health check & task orchestration |
| **Runtime & Language** | Python 3.12+ | Core application logic, typed with PEP 695 / Pydantic |
| **Package Management** | `uv` | Dependency resolution, virtualenv management, and lockfile (`uv.lock`) |
| **AI Gateway** | OpenRouter | Unified API endpoint for LLM inference |
| **AI Models** | Configurable via OpenRouter | Intent parsing, itinerary generation, multi-lingual replies |
| **Data Validation** | `Pydantic` v2 | Strict JSON schema parsing of AI outputs and domain models |
| **Live Travel Data** | SerpApi | Google Flights, Google Hotels, Google Travel Explore, Google Maps/Local |
| **Reverse-Budget Engine** | Custom Python | Allocates target budget into transit, stay, food, activities, contingency |
| **Optimization Engine** | Custom Python | 4-step downgrade & retry algorithm when target budget is exceeded |
| **Estimation Layer** | Custom Python + JSON tables | Heuristic rate matrices for local food, transit, and corridors |
| **Virtual Ledger** | Custom Python + PostgreSQL | Real-time budget tracking (Allocated, Planned, Spent, Remaining) |
| **Fallback Data** | Static JSON | Pre-compiled transit routes and demo safety nets for zero-API scenarios |
| **Database & Cache** | Supabase (PostgreSQL) | Persistence (Users, Trips, Ledgers) + SerpApi response cache with TTL |
| **HTTP Client** | `httpx` (AsyncClient) | Non-blocking calls to SerpApi and OpenRouter |
| **Configuration** | `pydantic-settings` + `.env` | Strictly-typed environment variables and API secrets |
| **Rate Protection** | Token bucket / Leaky bucket | Prevents SerpApi quota exhaustion and runaway costs |
| **Testing** | `pytest` + `pytest-asyncio` | Unit tests for budget math and integration mocks |
| **Containerization** | Docker + Docker Compose | Portable container deployment |
| **Version Control** | Git + GitHub | CI workflows and repository management |
| **Booking & Execution**| Deep links / Direct links | Reroutes users directly to airline/hotel/aggregator checkout |
| **Payments (Optional)**| Telegram Payments API | Optional in-app payment / deposit mechanism |

---

## 3. High-Level Flow
```
User (Telegram) 
     │
     ▼
FastAPI Webhook (/webhook) ──► python-telegram-bot (Update Queue)
                                     │
                                     ▼
                              Intent Extractor (OpenRouter + Pydantic)
                                     │
                         ┌───────────┴───────────┐
                         ▼                       ▼
              Cache Check (Postgres)       SerpApi (Flights/Hotels/Maps)
                         └───────────┬───────────┘
                                     ▼
                         Reverse-Budget Engine & 4-Step Optimizer
                                     ▼
                         Virtual Ledger Update (Allocated/Spent)
                                     ▼
                         Response & Interactive Itinerary (Telegram Cards)
```
