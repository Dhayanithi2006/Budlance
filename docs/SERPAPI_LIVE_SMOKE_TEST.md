# BUDLANCE — SerpApi Live Smoke Test Report

**Date**: 2026-10-04  
**Scenario**: Chennai (MAA) → Goa (GOI), 2 adults, round-trip, 4 nights  
**Outbound**: 2026-11-03 | **Return**: 2026-11-07  
**Budget**: ≤5 live searches  
**API key**: REDACTED (stored in `.env`)

---

## Credit Accounting

| Checkpoint       | Searches Left |
|------------------|--------------|
| **BEFORE test**  | 248          |
| **AFTER test**   | 244          |
| **Consumed**     | **4**        |

Account API checks are **FREE** — they did not consume any search quota.

---

## Engine Results

### Engine 1 — `google_travel_explore` — SUCCESS

| Field | Value |
|-------|-------|
| Status | SUCCESS |
| `departure_id` | `MAA` (Chennai) |
| Destinations returned | **88** |
| Sample | New Delhi, Mumbai, Singapore, Bengaluru, Dubai |
| Goa present | Yes |

Correct parameter used: `departure_id=MAA`
Live data confirmed: 88 real destination objects returned from SerpApi.

---

### Engine 2 — `google_flights` — SUCCESS

| Field | Value |
|-------|-------|
| Status | SUCCESS |
| `departure_id` | `MAA` |
| `arrival_id` | `GOI` |
| `outbound_date` | 2026-11-03 |
| `return_date` | 2026-11-07 |
| `adults` | 2 |
| Best flights | **3** |
| Other flights | **6** |

**Sample live flights (2 adults, round-trip, INR):**

| Airline | Flight No. | Round-Trip Price |
|---------|-----------|-----------------|
| IndiGo  | 6E 588    | Rs.23,300       |
| IndiGo  | 6E 6815   | Rs.26,060       |
| IndiGo  | 6E 562    | Rs.26,060       |

Live data confirmed: Real airline names, real flight numbers, real INR prices.

---

### Engine 3 — `google_hotels` — SUCCESS

| Field | Value |
|-------|-------|
| Status | SUCCESS |
| `q` | `Hotels in Goa` |
| `check_in_date` | 2026-11-03 |
| `check_out_date` | 2026-11-07 |
| `adults` | 2 |
| Properties returned | **20** |

**Sample live hotels (4 nights, 2 adults, INR):**

| Hotel | Class | Per Night | Total (4N) | Rating |
|-------|-------|-----------|------------|--------|
| Hilton Goa Resort | 5-star | Rs.13,174 | Rs.52,696 | 4.5 |
| The Crown Goa | 5-star | Rs.7,035 | Rs.28,140 | 4.3 |
| Hyatt Centric Candolim Goa | 5-star | Rs.9,478 | Rs.37,910 | 4.4 |

Live data confirmed: Real hotel names, real per-night rates, real aggregate totals.

---

### Engine 4 — `google_maps` (Local Places) — SUCCESS

| Field | Value |
|-------|-------|
| Status | SUCCESS |
| `q` | `places attractions in Goa` |
| `ll` | `@15.2993,74.1240,12z` |
| Local results | **20** |

**Sample live places:**

| Place | Rating | Type |
|-------|--------|------|
| Dudhsagar Falls | 4.6 | Tourist attraction |
| Fort Aguada | 4.2 | Tourist attraction |
| Velsao Beach | 4.4 | Tourist attraction |

Live data confirmed: Real attraction names, ratings, and categories.
Note: `q` key is correct but runtime uses non-standard `location` key — see Bug 4 below.

---

## Source-Trace Analysis

### Runtime flow (as designed)

```
runtime
  --> CacheFallbackManager.get_travel_data()
        --> Cache check (Supabase)
        --> SerpApiGateway.execute_search()
              --> live SerpApi (https://serpapi.com/search.json)
                    --> normalizer (normalize_flights / normalize_hotels / normalize_places)
                          --> FlightOption / HotelOption / PlaceOption
                                --> application response
```

### What is confirmed correct

| Component | Status |
|-----------|--------|
| `SerpApiGateway.execute_search()` | Authenticates and calls live API correctly |
| `CacheFallbackManager` cache check | Correctly consults Supabase before live call |
| `retry_with_backoff` wrapping | Correctly wraps all live calls |
| `normalize_flights()` | Correctly keys on `best_flights`, `other_flights`, leg structure |
| `normalize_hotels()` | Correctly keys on `properties`, `rate_per_night`, `total_rate` |
| `normalize_places()` | Correctly keys on `local_results` |

---

## CRITICAL: Parameter Bugs Found (Orchestrator)

> All 3 primary engines silently fail at the SerpApi layer.
> Hardcoded fallback data is served to users today — live API results never reach the runtime.

### Bug 1 — `google_travel_explore` — wrong param key

**File**: `orchestrator.py:1155-1159` (`_discover_destinations`)

```python
# CURRENT (BROKEN) — SerpApi ignores unknown 'origin' key
params={"origin": origin, "budget": float(budget), "interests": ",".join(interests)}

# REQUIRED
params={"departure_id": "MAA"}  # IATA airport code
```

Consequence: SerpApi returns error / empty response.
Fallback activated: Hardcoded catalog `["Goa","Jaipur","Udaipur","Kerala","Ooty","Coorg","Manali"]`

---

### Bug 2 — `google_flights` — wrong param keys

**File**: `orchestrator.py:1315-1318` (`lookup_transport_options`)

```python
# CURRENT (BROKEN)
params={"origin": origin, "destination": destination, "people": people}

# REQUIRED
params={
    "departure_id":  "MAA",
    "arrival_id":    "GOI",
    "outbound_date": "2026-11-03",
    "return_date":   "2026-11-07",
    "adults":        2,
    "type":          "1",
}
```

Consequence: No flights returned from SerpApi.
Fallback activated: Hardcoded `FlightOption(airline="IndiGo", flight_number="6E-101", price=Rs.4,000/leg)` — `orchestrator.py:1343-1364`

---

### Bug 3 — `google_hotels` — wrong param keys

**File**: `orchestrator.py:1488-1492` (`_collect_travel_components`)

```python
# CURRENT (BROKEN)
params={"destination": destination, "days": days, "people": people}

# REQUIRED
params={
    "q":              "Hotels in Goa",
    "check_in_date":  "2026-11-03",
    "check_out_date": "2026-11-07",
    "adults":         2,
}
```

Consequence: No hotels returned from SerpApi.
Fallback activated: Hardcoded `HotelOption(name="{dest} Heritage Palace", price_per_night=Rs.3,000)` — `orchestrator.py:1495-1504`

---

### Bug 4 — `google_maps` — `location` is not a valid SerpApi param

**File**: `orchestrator.py:678-682`

```python
# CURRENT — 'location' silently ignored
params={"q": "places attractions in Goa", "location": "Goa"}

# BETTER — use ll for geo-precision
params={"q": "places attractions in Goa", "ll": "@15.2993,74.1240,12z"}
```

Consequence: Results may be returned without geographic filtering.
`q` IS correct so some results are returned, but city-level precision is lost.

---

### Bug 5 — `CacheFallbackManager` cross-pollination

**File**: `cache/manager.py:164-179`

On a live `google_flights` call failure, the manager falls back to `get_train_corridor()`.
A failed flight query can **return train corridor data** — mismatched engine/data.

---

## Hardcoded Data Verdict

| Engine | Live Data Reaches Runtime? | What runtime actually serves |
|--------|---------------------------|------------------------------|
| `google_travel_explore` | NO | Static list: Goa, Jaipur, Udaipur, Kerala, Ooty, Coorg, Manali |
| `google_flights` | NO | Hardcoded IndiGo 6E-101 @ Rs.4,000/leg |
| `google_hotels` | NO | Hardcoded "{dest} Heritage Palace" @ Rs.3,000/night |
| `google_maps` | PARTIAL | Live results returned but without geo-filtering |

All hardcoded travel/provider data is actively reaching the runtime response today.
The SerpApi gateway itself works perfectly — the bugs are in the orchestrator parameter mappings.

---

## Recommended Fixes (DO NOT START UNTIL APPROVED)

1. **Orchestrator** — map city names to IATA codes before calling `google_flights` / `google_travel_explore`
2. **Orchestrator** — derive `check_in_date`, `check_out_date` from trip start date + days; pass `adults` to `google_hotels`
3. **Orchestrator** — replace `location` key with `ll` (lat/lng/zoom) in `google_maps` calls
4. **CacheFallbackManager** — separate train fallback from flight engine path (no cross-pollination)
5. **After fixes** — remove hardcoded IndiGo 6E-101, Heritage Palace, and static destination catalog from runtime paths

---

> STOP — Do not begin any cleanup or fixes until explicitly approved.
