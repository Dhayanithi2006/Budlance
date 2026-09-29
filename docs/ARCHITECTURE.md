# Budlance — Architecture Reference

> **The user's budget is the primary constraint.**  
> Every destination, hotel, transport option, activity, and replanning decision must
> satisfy budget feasibility before being committed to an itinerary.

---

## 1. End-to-End Planning Flow

```
Telegram message
  └─► POST /webhook (FastAPI)
        └─► AI Intent Layer  ──── parse_trip_intent() → ParsedTripIntent
              └─► Budlance Orchestrator
                    │
                    ├─► Destination missing? → google_travel_explore → candidate list
                    │
                    └─► For each candidate destination:
                          └─► _collect_travel_components()
                                ├─► google_flights   → FlightOption / TransitOption
                                ├─► trains (fallback)→ TransitOption [FALLBACK]
                                ├─► google_hotels    → HotelOption
                                ├─► google_maps      → PlaceOption list
                                └─► google_maps_directions → RouteOption
                          └─► Estimation Layer
                                ├─► estimate_food()         [ESTIMATED]
                                └─► estimate_local_transit() [ESTIMATED]
                          └─► Reverse-Budget Engine
                                ├─► FEASIBLE → commit plan
                                └─► NOT FEASIBLE → Optimization Engine
                                      └─► max 4 attempts (frozen sequence — see §5)
                                            └─► loop back to Cache/SerpApi
                    │
                    (after first FEASIBLE candidate)
                    │
                    ├─► Persistence sequence
                    │     User → Trip → TripIntent → Itinerary → BudgetAllocation → LedgerEntries
                    └─► Format Telegram response → reply
```

---

## 2. Rescue Flow

```
Telegram message (during active trip)
  └─► POST /webhook (FastAPI)
        └─► AI Intent Layer  ──── parse_rescue_intent() → ParsedRescueIntent
              └─► Budlance Orchestrator
                    └─► rescue_type == "weather_closure" or "price_dispute"?
                          └─► RescueService.execute_rescue(chat_id, message)
                                ├─► Load active Trip from Supabase (by chat_id)
                                ├─► Load Itinerary + LedgerEntries
                                │
                                ├─► weather_closure:
                                │     └─► google_maps → alternatives → mini feasibility
                                │           ├─► FEASIBLE: update itinerary + ledger + RescueEvent
                                │           └─► NOT FEASIBLE: return original itinerary intact
                                │
                                └─► price_dispute:
                                      └─► Estimation rate tables (NO SerpApi calls)
                                            └─► FareGuidance + optional LedgerEntry (USER_REPORTED)
```

---

## 3. Layer Reference

| Layer | Module | Boundary |
|-------|--------|----------|
| **Telegram** | `bot/handlers.py`, `bot/bot.py` | No business logic — dispatches to Orchestrator |
| **FastAPI** | `api/app.py`, `api/routes.py` | `/health`, `/webhook` only |
| **AI Intent** | `ai/service.py`, `ai/client.py`, `ai/schemas.py` | Intent extraction only — **not** authoritative for math |
| **Orchestrator** | `orchestrator/orchestrator.py` | Coordinates services — contains no formulas |
| **Cache/Fallback** | `cache/manager.py`, `cache/fallback.py` | Resolution: Cache → Fallback JSON → SerpApi live |
| **SerpApi Gateway** | `serpapi/client.py`, `serpapi/rate_limiter.py` | Rate limited, retry/backoff, usage tracking |
| **Normalizer** | `normalization/normalizer.py` | SerpApi raw JSON → FlightOption / HotelOption / PlaceOption / RouteOption |
| **Estimation** | `estimation/estimator.py` | Food and local transit heuristics — clearly labeled ESTIMATED |
| **Budget Engine** | `engine/budget.py` | **Financial authority** — feasibility gate |
| **Optimizer** | `engine/optimizer.py` | 4-step downgrade sequence (frozen) |
| **Itinerary** | `itinerary/generator.py` | Created **only** after FEASIBLE decision |
| **Ledger** | `ledger/manager.py` | Tracks Allocated / Planned / Spent / Remaining |
| **Rescue** | `rescue/service.py` | Modifies existing active trip — no second planner |
| **Persistence** | `db/repositories/` | User → Trip → Intent → Itinerary → Ledger |

---

## 4. Data Provenance Labels

Every cost and data item in Budlance carries an explicit source label:

| Label | Meaning |
|-------|---------|
| `LIVE` | Returned by SerpApi in the current request |
| `CACHED` | Returned from the in-memory/database cache |
| `FALLBACK` | Static JSON dataset (train/bus corridors, destination catalog) |
| `ESTIMATED` | Heuristic from EstimationLayer (food, local auto/cab) |
| `USER_REPORTED` | Amount stated by the user during a live trip (e.g. fare dispute) |

These labels are preserved from normalizer output through to Telegram response formatting.

---

## 5. Frozen 4-Step Optimization Sequence

When the Reverse-Budget Engine returns NOT_FEASIBLE, the OptimizationEngine applies
downgrades in this exact order — the sequence is frozen and must not be changed:

```
Attempt 1  →  Hotel tier down       (select next-lower hotel_class option)
Attempt 2  →  Transport class down  (select next-cheaper transport option)
Attempt 3  →  Reduce trip length    (duration_days - 1, minimum 1)
Attempt 4  →  Trim Bucket B         (reduce discretionary allocation)
```

After each downgrade, the full evaluation loop repeats:
`Cache/Fallback → SerpApi if needed → Normalize → Estimate → Reverse-Budget Engine`

If all four attempts fail, the response is `NOT_FEASIBLE` with an explicit deficit and
recommendation. No itinerary or ledger is created.

---

## 6. Waterfall Budget Logic

```
Total Budget (user declared)
  └─► Bucket D (Rescue Reserve — 10% of total budget)
        └─► Bucket A (Fixed: transport + hotel)
              └─► Bucket B (Survival: food + local transit)
                    └─► Bucket C (Discretionary: activities)
                          └─► Remaining surplus
```

**Invariant:** `Bucket_A + Bucket_B + Bucket_C + Bucket_D ≤ Total Budget`

The Reverse-Budget Engine is the sole authority for this calculation.
The AI layer never performs authoritative budget arithmetic.

---

## 7. Virtual Ledger

The Virtual Ledger tracks the financial state of an active trip:

| Field | Meaning |
|-------|---------|
| `total_budget` | User-declared budget (immutable) |
| `total_allocated` | Sum of all planned expenditures |
| `total_spent` | User-reported real spending |
| `remaining_budget` | `total_budget − total_allocated` |

**The Virtual Ledger is not a bank account.** It does not process payments, hold funds,
or guarantee prices. Actual booking and payment occur through external providers.

---

## 8. Booking and Payment Boundaries

- **Booking**: Budlance plans and may generate deep links to airlines/hotels. The external
  provider executes the actual booking.
- **Payment**: Not implemented. Telegram Payments API integration is a future optional
  extension noted in TECH_STACK.md.
- **Price guarantees**: Budlance uses live SerpApi prices as inputs. Prices shown are
  point-in-time data — final prices are subject to provider availability.

---

## 9. Fallback Data Boundaries

Static fallback data is used when neither a cache hit nor a live SerpApi result is
available. Fallback data:

- Is **explicitly labeled `FALLBACK`** in all models and Telegram responses.
- Covers train/bus corridors from static JSON files in `data/`.
- Is used only as a last resort — the architecture prefers live SerpApi data.
- Does **not** include guaranteed rail coverage for all routes.

---

## 10. Key Architectural Constraints

1. The Orchestrator coordinates — it contains no budget formulas.
2. The Budget Engine decides feasibility — not the AI.
3. The Rescue Service reuses the existing trip — it does not create a second planner.
4. The Telegram handler contains no business logic.
5. No secrets are hardcoded. All credentials are injected via environment variables.
6. The 4-step optimization sequence is frozen and must not be reordered.
