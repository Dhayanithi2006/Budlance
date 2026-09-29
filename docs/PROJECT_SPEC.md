# Budlance — Complete Architecture & Project Specification

## 1. Core Idea

> **Budlance is a reverse-budget AI travel agent.**

The user starts with:
```text
Budget
+ Number of people
+ Duration
+ Destination OR travel preferences
+ Interests
```

Budlance then executes:
```text
Discover
   ↓
Collect travel data
   ↓
Calculate complete cost
   ↓
Check feasibility
   ↓
Optimize if over budget
   ↓
Generate trip
   ↓
Track budget
   ↓
Replan when needed
```

The **Reverse-Budget Engine is the center of the architecture**.

---

## 2. High-Level Architecture

```text
                           ┌──────────────────┐
                           │      USER        │
                           │ Telegram Message │
                           └────────┬─────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │   TELEGRAM BOT     │
                          │ python-telegram-  │
                          │ bot                │
                          └────────┬───────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │   FASTAPI BACKEND  │
                          └────────┬───────────┘
                                    │
                 ┌──────────────────┴──────────────────┐
                 │                                     │
                 ▼                                     ▼
       ┌────────────────────┐               ┌───────────────────┐
       │   AI INTENT LAYER  │               │    RESCUE MODE    │
       │    OpenRouter      │               │ Existing Trip     │
       │                    │               │ Trigger           │
       └─────────┬──────────┘               └─────────┬─────────┘
                 │                                    │
                 │                         Load Trip + Ledger
                 │                                    │
                 └────────────────┬───────────────────┘
                                  ▼
                       ┌────────────────────┐
                       │ BUDLANCE           │
                       │ ORCHESTRATOR       │
                       └─────────┬──────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │ CACHE / FALLBACK       │
                    │ MANAGER                │
                    └───────────┬────────────┘
                       ┌────────┼────────┐
                       │        │        │
                  Cache Hit  Fallback   None
                       │        │        │
                       └────────┼────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │   SERPAPI       │
                       │     GATEWAY     │
                       └────────┬────────┘
                                │
             ┌──────────────────┼───────────────────┐
             │          │       │        │          │
             ▼          ▼       ▼        ▼          ▼
          Travel      Flights Hotels   Local      Search
          Explore                         /Maps
                                     
                                │
                                ▼
                       ┌─────────────────┐
                       │ DATA NORMALIZER │
                       └────────┬────────┘
                                │
                    ┌───────────┴───────────┐
                    │                       │
                    ▼                       ▼
             Live SerpApi Data       Static/Fallback
                                      Train/Bus JSON
                    │                       │
                    └───────────┬───────────┘
                                ▼
                     ┌─────────────────────┐
                     │ ESTIMATION LAYER    │
                     │ Food / Local Travel │
                     └──────────┬──────────┘
                                ▼
                  ┌────────────────────────────┐
                  │ REVERSE-BUDGET ENGINE      │
                  └─────────────┬──────────────┘
                                │
                         ┌──────┴──────┐
                         ▼             ▼
                     FEASIBLE      NOT FEASIBLE
                         │             │
                         │             ▼
                         │    ┌─────────────────┐
                         │    │ OPTIMIZATION    │
                         │    │ ENGINE          │
                         │    └────────┬────────┘
                         │             │
                         │       Max 4 attempts
                         │             │
                         │             ▼
                         │    Apply One Downgrade
                         │             │
                         │             └──────► Cache/Fallback
                         │
                         ▼
                  ┌────────────────┐
                  │ ITINERARY      │
                  │ GENERATOR      │
                  └───────┬────────┘
                          ▼
                  ┌────────────────┐
                  │ VIRTUAL LEDGER │
                  └───────┬────────┘
                          │
                          ▼
                 ┌───────────────────┐
                 │ SUPABASE POSTGRES │
                 └─────────┬─────────┘
                           │
                           ▼
                  ┌────────────────┐
                  │ TELEGRAM       │
                  │ RESPONSE       │
                  └────────────────┘
```

---

## 3. Layer 1 — Telegram

### Technology
* Telegram Bot API
* `python-telegram-bot`

This is the **interface**, not the intelligence.

The bot receives:
```text
"I have ₹15,000"
"5 days"
"3 people"
"I like beaches"
"Plan Kerala"
```

It can also receive later messages such as:
```text
"It's raining"
"The auto is asking ₹500"
"This place is closed"
```

---

## 4. Layer 2 — FastAPI

FastAPI is the application's backend gateway.

```text
Telegram
   ↓
FastAPI
   ↓
Budlance services
```

Responsibilities:
* Telegram webhook
* Request validation
* Orchestration
* Health endpoint
* Service coordination
* Error handling

It does **not** contain the entire planning logic.

---

## 5. Layer 3 — AI Intent Layer

### Technology
* OpenRouter
* Selected model
* Pydantic

The AI converts natural language into structured intent.

Example:
```json
{
  "budget": 15000,
  "currency": "INR",
  "people": 3,
  "days": 5,
  "origin": "Chennai",
  "destination": "Kerala",
  "interests": [
    "beaches",
    "calm_places",
    "local_food"
  ],
  "traveler_type": "family"
}
```

The AI is responsible for:
* Language understanding
* Intent extraction
* Multilingual interaction
* Conversational responses
* Interpreting rescue messages

The AI is **not** the authority for budget arithmetic.

---

## 6. Layer 4 — Budlance Orchestrator

The orchestrator determines what information is actually required.

For example:
```text
User wants:
Kerala + beaches + food

Required:
Destination discovery
Travel
Hotel
Places
Food signals
Routes
```

It then sends those requirements to the appropriate services.
The orchestrator avoids calling every SerpApi engine for every request.

---

## 7. Layer 5 — Cache/Fallback Manager

This layer is explicitly part of the architecture because SerpApi usage is metered.

The resolution sequence:
```text
             REQUEST
                ↓
          CACHE CHECK
          ↙          ↘
       FOUND        NOT FOUND
         ↓              ↓
      USE CACHE    FALLBACK CHECK
                       ↙     ↘
                  AVAILABLE  NONE
                      ↓        ↓
                  USE JSON   SERPAPI
```

This means repeated searches reuse existing information.

### Fallback JSON
Used for controlled cases such as:
```text
Train
Bus
Demo corridor data
```
Those results are explicitly labeled **fallback**, not live SerpApi data.

---

## 8. Layer 6 — SerpApi Gateway

This is the **live external data layer**.

The architecture uses the SerpApi engines that are relevant to Budlance:
* `Google Travel Explore`: Destination discovery
* `Google Flights`: Flight options and pricing
* `Google Hotels`: Hotel options and pricing
* `Google Local / Maps`: Beaches, restaurants, attractions
* `Google Maps Directions`: Distance, routes, travel time
* `Google Maps Reviews`: Qualitative venue signals
* `Google Search`: Fallback information not covered by dedicated engines

Not every request needs every engine.
SerpApi's result **must affect the actual trip plan**, not merely appear somewhere in the UI.

---

## 9. Layer 7 — Rate Limiter + Retry

Every live SerpApi request passes through:
```text
Request
 ↓
Rate limit
 ↓
API call
 ↓
Failure?
 ↓
Retry with backoff
```

The system also records API usage:
```text
Trip ID: 1024

Flights: 1
Hotels: 1
Local: 0 (cache)
Search: 0

Total live calls: 2
```

This enables measuring real cost efficiency.

---

## 10. Layer 8 — Data Normalizer

SerpApi responses do not flow directly into the budget engine.

```text
SerpApi Raw JSON
       ↓
Data Normalizer
       ↓
Internal Models (FlightResult, HotelResult, PlaceResult, RouteResult)
```

The Budget Engine works exclusively with these internal models.

---

## 11. Layer 9 — Estimation Layer

Some trip expenses do not have reliable live prices. Budlance explicitly separates:

* **Live**: Hotel price, flight price, place details
* **Estimated**: Food heuristics, local auto/cab fares
* **Fallback**: Train fare, bus fare (static corridors)
* **User-Reported**: Real-time spending reports ("Auto driver asked ₹500")

This distinction is essential for transparency and trust.

---

## 12. Layer 10 — Reverse-Budget Engine

This is **Budlance's core**.
Its job is: **“Can we construct this trip within the user's budget?”**

### Input
* Total budget
* People
* Days
* Travel cost
* Hotel cost
* Activities
* Food estimate
* Local transport estimate
* Rescue reserve

### Output
* `FEASIBLE` or `NOT_FEASIBLE`

---

## 13. Waterfall Budget Logic

```text
Total Budget
      ↓
Reserve Rescue Fund
      ↓
Subtract known fixed costs (Travel + Stay)
      ↓
Remaining budget
      ↓
Check minimum daily allowance
      ↓
Allocate food/local expenses
      ↓
Allocate activities/discretionary money
      ↓
Final feasibility
```

The engine guarantees:
`Total Allocations <= User Budget`

---

## 14. Layer 11 — Optimization Engine

When a plan exceeds budget:
```text
NOT FEASIBLE
      ↓
Optimization Engine (Max 4 attempts)
```

### Frozen Downgrade Sequence
1. **Attempt 1**: Hotel tier down ➔ Recheck
2. **Attempt 2**: Transport class down ➔ Recheck
3. **Attempt 3**: Reduce trip length (by 1 day) ➔ Recheck
4. **Attempt 4**: Trim discretionary Bucket B ➔ Recheck

### Critical Loop
After each downgrade:
```text
Apply Downgrade
      ↓
Cache/Fallback
      ↓
SerpApi if required
      ↓
Normalize
      ↓
Reverse-Budget Engine
```

---

## 15. If Still Impossible

After four attempts:
```text
Still Not Feasible ──► Telegram Response
```
The bot explicitly communicates trade-offs (e.g. "This trip still doesn't fit ₹15,000. Consider reducing by 1 day or adjusting budget to ₹18,200."). No silent failures.

---

## 16. Layer 12 — Itinerary Generator

Generated **after the financial feasibility decision**:
* Takes selected destination, travel, stay, attractions, food/transit allocations, routes, and duration.
* Produces day-by-day itinerary with time, place, activity, route, estimated cost, and budget status.

---

## 17. Layer 13 — Virtual Ledger

Tracks allocations without acting as a bank account:
* `Allocated`
* `Planned`
* `Spent/Reported`
* `Remaining`

Persisted in Supabase PostgreSQL to survive across Telegram conversations.

---

## 18. Layer 14 — Supabase PostgreSQL Schema

Entities:
* `users`
* `trips`
* `trip_intents`
* `trip_options`
* `flight_options`
* `hotel_options`
* `place_options`
* `itineraries`
* `budget_allocations`
* `ledger_entries`
* `plan_attempts`
* `rescue_events`
* `search_cache`
* `api_usage`

Entity Relationship:
```text
User
 ↓
Trip
 ├── Intent
 ├── Options
 ├── Itinerary
 ├── Ledger
 ├── Plan Attempts
 └── Rescue Events
```

---

## 19. Rescue Mode

Rescue Mode enters the existing planning system using current trip state:

```text
User: "It's raining" / "Auto asking ₹500"
        ↓
Load active trip + ledger
        ↓
AI Rescue Intent
        ↓
Rescue Type?
```

* **Weather / Closure**: Local/Maps search ➔ Alternative places ➔ Mini feasibility ➔ Update itinerary & ledger.
* **Price Dispute**: Fare logic (no SerpApi needed) ➔ Fair-price guidance ➔ Optional ledger update.

---

## 20. Booking & Payment Boundaries

* **Booking**: Budlance plans and provides external deep links. The external provider executes final booking.
* **Payment**: Optional, isolated from Reverse-Budget Engine. No raw card storage.

---

## 21. Complete Planning & Rescue Flows

### Planning Flow
```text
USER ──► Telegram ──► FastAPI ──► AI Intent Parser ──► Structured Trip Request
  ──► Cache / Fallback Check
        ├── Found ──► Normalize & Continue
        └── Not Found ──► SerpApi ──► Data Normalizer
  ──► Estimation Layer
  ──► Reverse-Budget Engine
        ├── FEASIBLE ──► Itinerary Generator ──► Virtual Ledger ──► Supabase ──► Telegram
        └── NOT FEASIBLE ──► Optimization Engine (max 4 attempts) ──► Loop to Cache/SerpApi
```

### Rescue Flow
```text
USER ("It's raining") ──► Telegram ──► FastAPI ──► Load Active Trip + Ledger
  ──► AI Rescue Intent
  ──► Rescue Type (Weather/Closure vs Price Dispute)
  ──► Maps Search OR Fare Logic
  ──► Mini Feasibility ──► Update Ledger & Itinerary ──► Telegram
```

---

## 22. Frozen Technical Stack

* **Language**: Python 3.12+
* **Package Manager**: `uv`
* **Web Framework**: FastAPI
* **Bot Framework**: `python-telegram-bot`
* **AI Gateway**: OpenRouter
* **Schema Validation**: Pydantic
* **HTTP Client**: `httpx`
* **Live Travel Data**: SerpApi
* **Database & Cache**: Supabase PostgreSQL
* **Testing**: `pytest`
* **Containerization**: Docker
* **Version Control**: Git + GitHub

---

## 23. The Cardinal Architectural Rule

> **The user's budget is the primary constraint.**

Every destination, hotel, transport option, activity, and replanning decision must satisfy budget feasibility before being committed to an itinerary.
