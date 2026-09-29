# Budlance — SerpApi Integration Reference

## Overview

Budlance uses SerpApi as its **live external travel data layer**. SerpApi results are not
decorative — they directly feed the Data Normalizer, which feeds the Reverse-Budget Engine.
Changing the SerpApi results changes feasibility decisions and trip plans.

---

## Implemented SerpApi Engines

| Engine key | SerpApi engine | Role in Budlance |
|------------|---------------|-----------------|
| `google_travel_explore` | Google Travel Explore | Destination discovery when the user has not specified one |
| `google_flights` | Google Flights | Flight options and per-person pricing |
| `google_hotels` | Google Hotels | Hotel options, class/tier, nightly rate, total stay cost |
| `google_maps` | Google Maps Local | Nearby attractions, beaches, restaurants, places of interest |
| `google_maps_directions` | Google Maps Directions | Route summary, distance, estimated travel time |
| `trains` | Static JSON fallback | Train/bus corridor data (not a live SerpApi call) |

> **Note:** `google_maps_reviews` and `google_search` are referenced in the architecture
> specification but are not currently implemented as active engine calls. Only the engines
> in the table above are called by the current codebase.

---

## How SerpApi Results Flow Through the System

```
CacheFallbackManager.get_travel_data(engine, params)
  │
  ├─► Cache hit?  → return TravelDataEnvelope(source=CACHED)
  ├─► Fallback?   → return TravelDataEnvelope(source=FALLBACK)
  └─► Live call   → SerpApiClient → SerpApi API → TravelDataEnvelope(source=LIVE)
        │
        └─► DataNormalizer
              ├─► normalize_flights()   → list[FlightOption]   (price, airline, source=LIVE)
              ├─► normalize_hotels()    → list[HotelOption]    (total_price, hotel_class, source=LIVE)
              ├─► normalize_places()    → list[PlaceOption]    (name, rating, source=LIVE)
              └─► normalize_routes()   → list[RouteOption]    (distance, duration, source=LIVE)
                    │
                    └─► ReverseBudgetEngine.evaluate(
                              transport=FlightOption,   ← live SerpApi price
                              hotel=HotelOption,        ← live SerpApi price
                              food_estimate=...,        ← ESTIMATED
                              local_transit=...,        ← ESTIMATED
                              activities_budget=...,    ← derived from total budget
                          )
                          → BudgetEvaluationResult (FEASIBLE or NOT_FEASIBLE)
```

---

## Why SerpApi Materially Affects Budlance

SerpApi data is **not cosmetic**. It is the input to the budget decision:

1. **Flight price from SerpApi** → `transport_cost` in the budget breakdown.
   A more expensive flight directly increases Bucket A (Fixed costs) and can push
   a trip from FEASIBLE to NOT_FEASIBLE.

2. **Hotel price from SerpApi** → `hotel_cost` in the budget breakdown.
   The optimizer's first downgrade attempt (hotel tier down) selects a lower-class
   hotel from the SerpApi hotel list — if no lower option exists, it falls back to
   static alternatives.

3. **Destination discovery from SerpApi** → candidate destination list.
   If the user does not specify a destination, `google_travel_explore` determines
   which destinations are evaluated by the budget engine.

4. **Local places from SerpApi** → itinerary day items.
   Places returned by `google_maps` appear in the day-by-day schedule with their
   live rating and category data.

### Verified in tests

`test_serpapi_material_contribution` in `tests/test_orchestrator_and_integration.py`
verifies that:

- **Tight budget + high SerpApi prices** → `NOT_FEASIBLE` (the live price makes the trip
  unaffordable).
- **Adequate budget + same SerpApi prices** → `FEASIBLE`, and the exact live flight/hotel
  price appears in `budget_breakdown.transport_cost` and `budget_breakdown.hotel_cost`.

---

## Cache / Fallback Resolution Order

```
1. In-memory / Supabase cache (TTL-based)
      ↓ miss
2. Static fallback JSON (data/ directory — trains, buses, corridor data)
      ↓ no fallback entry
3. Live SerpApi call (rate-limited, with exponential backoff retry)
```

All three paths return a `TravelDataEnvelope` with an explicit `source` field:
`LIVE`, `CACHED`, or `FALLBACK`.

---

## Rate Limiting and Reliability

- A **token-bucket rate limiter** limits concurrent SerpApi calls to prevent quota exhaustion.
- Failed live calls are retried with **exponential backoff** (transient errors only).
- Non-transient errors (authentication failures, invalid API key) fail immediately without
  retry.
- API usage is tracked per trip in the `api_usage` table (engine, call count, trip_id).

---

## What Budlance Does Not Claim

- **No booking**: SerpApi prices are point-in-time. Budlance does not book flights or hotels.
- **No guaranteed final prices**: Live prices change. SerpApi results are used for planning
  feasibility, not as binding purchase quotes.
- **No guaranteed rail/bus coverage**: Train and bus data comes from static fallback JSON,
  not live SerpApi or rail APIs. Coverage is limited to pre-compiled corridors.
- **No reviews engine active**: `google_maps_reviews` is documented in the spec but not
  currently implemented as an active engine call.
