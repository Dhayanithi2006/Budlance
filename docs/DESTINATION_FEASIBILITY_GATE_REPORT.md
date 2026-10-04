# BUDLANCE — DESTINATION FEASIBILITY GATE REPORT
## Preventing Impossible / Unaffordable Destinations Before Selection & Enforcing Quota Guards

---

## 1. Executive Summary & Root Cause Analysis

### Background & Observed Incidents
1. **Initial Incident (Destination Hallucination):**
   When tested with the open-ended user input:
   > *"I have 15000 for 3 people for 2 days. I like to explore local food of that place and theme park from chennai"*
   The system previously selected **Tokyo** as the trip destination and attempted to make it feasible via invalid domestic train fallback logic and zero-cost/unverified accommodations.

2. **Secondary Incident (Quota Runaway & Over-Pruning):**
   In the subsequent live test:
   - Travel Explore returned 60+ worldwide candidates and dominated discovery.
   - Goa was wrongly pruned at Gate 1 because its Travel Explore flight price exceeded budget, even though an existing offline Chennai→Goa train corridor was available.
   - The unconstrained loop evaluated destination after destination, consuming ~118 SerpApi credits and triggering 60+ provider calls over 226 seconds.

### Root Causes
1. **Explore Dominance & Lack of Curated Domestic Priority:**
   Travel Explore returns broad, unfiltered global and regional candidates without prioritizing known domestic budget destinations with direct ground connectivity.
2. **Corridor-Blind Gate 1 Screening:**
   Gate 1 pruned candidates if Explore `flight_price * people > budget`, discarding viable ground corridor destinations (e.g. Goa, Ooty) whose train/bus fares are well within budget.
3. **Missing Quota & Provider Call Ceilings:**
   The evaluation loop lacked hard caps on candidates evaluated and provider calls dispatched per request.
4. **Lack of Early Exit:**
   The discovery engine continued evaluating candidates instead of halting immediately upon finding the first fully feasible plan.
5. **Synthetic Transport & Accommodation Hallucination (Fixed in Phase 1):**
   Synthetic generic Indian Railways trains were generated for international cities, and missing lodging was evaluated at ₹0.

---

## 2. Implemented Architecture: Feasibility Gate & Quota Guards

The destination discovery and evaluation pipeline now enforces strict priority ordering, corridor-aware screening, and hard quota safety bounds:

```
[Candidate Discovery]
  ├─ Curated Domestic Pool (Goa, Ooty, Coorg, Manali, Udaipur, Kerala) [PRIORITY 1 & 2]
  └─ Google Travel Explore (Low cost first, missing price last)         [PRIORITY 3 & 4]
       ↓
[Deterministic Ranking]
  1. Curated domestic with known offline corridor from origin
  2. Other curated domestic candidates
  3. Travel Explore candidates with valid low estimated cost
  4. Remaining Travel Explore candidates (missing price sorts last via sentinel)
       ↓
[Capped Candidate Loop]  ──► Hard Cap: MAX_LIVE_CANDIDATES_PER_REQUEST = 6
                         ──► Hard Cap: MAX_LIVE_PROVIDER_CALLS_PER_REQUEST = 10
                         ──► Timeout : DESTINATION_EVALUATION_TIMEOUT_SECONDS = 20s
       ↓
[Gate 1: Corridor-Aware Screening]
  ├─ If known offline corridor exists: DO NOT prune on flight metadata; pass to Gate 2.
  └─ If no corridor: prune if Explore flight_price * people > budget or hotel_price * nights > budget.
       ↓
[Gate 2: Physical Transport Connectivity Gate]
  ├─ If corridor exists: skip live flight call (saves provider budget); use offline train/bus corridor.
  └─ If no corridor: requires live flight with price > 0. (Zero synthetic trains / zero ₹0 transit).
       ↓
[Gate 3: Accommodation Verification Gate]
  └─ Multi-day trips require verified property with total_price > 0. No free stay, no fabricated names.
       ↓
[Gate 4: Reverse-Budget Feasibility Gate & Optimizer Safety]
  └─ Optimizer cannot downgrade or rescue invalid transport or missing hotel.
       ↓
[Early Exit]
  └─ Stop immediately upon FIRST fully feasible candidate! Never evaluate remaining candidates.
       ↓
[Gate 5: Interest / Place Discovery (Post-Feasibility Only)]
  └─ Live SerpApi Google Maps queries for user interests (local food, theme park) dispatched ONLY for the chosen feasible destination.
```

---

## 3. Detailed Component Implementations & Configuration

### 1. Corridor-Aware Gate 1
- **File:** `src/budlance/orchestrator/orchestrator.py` (`_discover_destinations`)
- Before pruning a candidate on Travel Explore `flight_price`, the system checks `_has_offline_corridor(origin, dest)`.
- If a valid offline train/bus corridor exists (e.g. Chennai → Goa, Chennai → Ooty), Gate 1 allows the candidate to pass to Gate 2.
- Gate 1 flight pruning applies **only** when no valid offline corridor exists.
- An invalid or missing corridor cannot rescue a candidate.

### 2. Curated Domestic Candidate Pool Restoration
- **File:** `src/budlance/orchestrator/orchestrator.py` (`_CURATED_DOMESTIC_POOL`)
- Restored original curated domestic destinations: **Goa, Ooty, Coorg, Manali, Udaipur, Kerala**.
- Placed FIRST in candidate discovery ahead of Travel Explore.
- Deduplication ensures candidates present in both pools are evaluated only once.
- Curated candidates pass through the identical Gate 1 → Gate 2 → lodging → budget feasibility pipeline; none are pre-approved or declared feasible without verification.

### 3. Deterministic Candidate Ranking
- Priority order:
  1. Curated domestic candidates with a valid known offline corridor from origin.
  2. Other curated domestic candidates.
  3. Travel Explore candidates with a valid low estimated cost.
  4. Remaining Travel Explore candidates.
- Missing prices are assigned `_MISSING_PRICE_SENTINEL = Decimal("999999")` to sort last without treating missing prices as ₹0 or free.

### 4. Hard Quota & Call Ceilings
- **File:** `src/budlance/config.py` & `src/budlance/orchestrator/orchestrator.py`
  - `MAX_LIVE_CANDIDATES_PER_REQUEST = 6`: Evaluates at most 6 candidates per user request.
  - `MAX_LIVE_PROVIDER_CALLS_PER_REQUEST = 10`: Never exceeds 10 live SerpApi provider calls.
  - Evaluation stops immediately if either ceiling is reached.

### 5. Skip Redundant Flight Calls for Corridor Candidates
- For curated candidates with a valid offline corridor, Google Flights is not called just to prove flight unaffordability. Gate 2 directly uses the offline ground corridor, saving SerpApi quota for lodging and details.

### 6. Early Exit
- Evaluation loop breaks immediately upon the first fully feasible candidate (`early_exit_reason = "FIRST_FEASIBLE"`).

### 7. Evaluation Timeout
- `DESTINATION_EVALUATION_TIMEOUT_SECONDS = 20.0`: Candidates exceeding 20 seconds are marked `TIMEOUT` and skipped without corrupting state or declaring invalid data feasible.

---

## 4. Verification & Mock Test Results

### 1. Mock-Only Invariant Suite (`tests/test_destination_feasibility_gate.py`)
**37 tests passing** (100% pass) covering all requested invariants:
- **Invariant A:** Goa appears in curated pool (`test_goa_in_curated_pool`).
- **Invariant B:** Goa survives Gate 1 with corridor despite expensive flight metadata (`test_goa_survives_gate1_with_corridor_despite_expensive_flight`).
- **Invariant C:** Tokyo has no offline corridor and cannot receive Indian rail fallback (`test_tokyo_has_no_offline_corridor`).
- **Invariant D:** Expensive flight with no corridor is pruned (`test_expensive_flight_no_corridor_is_pruned`).
- **Invariant E:** Missing flight price not treated as free and not pruned (`test_missing_flight_price_not_treated_as_free_and_not_pruned`).
- **Invariant F:** Curated candidates ordered before Explore (`test_curated_candidates_ordered_before_explore`).
- **Invariant G:** Round-trip transport = (outbound + return) × people (`test_round_trip_cost_calculation`).
- **Invariant H:** Optimizer cannot rescue invalid candidate (`test_optimizer_cannot_rescue_invalid_candidate`).
- **Invariant I:** First feasible candidate stops evaluation (`test_early_exit_on_first_feasible_candidate`).
- **Invariant J:** Candidate count capped at maximum (`test_candidate_count_capped_at_max`).
- **Invariant K:** Corridors return correct results (`test_has_offline_corridor_correct_results`).
- **Invariant L:** Missing/zero hotel prices never create HotelOption (`test_missing_and_zero_hotel_prices_never_create_hotel_option`).
- **Invariant M:** Invalid flight/hotel options never reach itinerary (`test_invalid_provider_options_never_reach_itinerary`).

### 2. Full Regression Suite Results
Executed with `SERPAPI_LIVE_ENABLED=false` to protect SerpApi quota:
```
======================= 636 passed, 2 warnings in 34.57s =======================
```
**All 636 tests passed** across the entire Budlance test suite with zero failures.

---

## 5. Exactly One Controlled Live E2E Verification (Real SerpApi)

Executed with `SERPAPI_LIVE_ENABLED=true` on 2026-10-04.

### Test Input
```
"I have 15000 for 3 people for 2 days. I like to explore local food of that place and theme park from chennai"
```

### Execution Telemetry & Performance
| Metric | Previous Run | Corrected Run | Change |
|---|---|---|---|
| **Runtime** | 226 seconds | **37.59 seconds** | **83.4% faster** |
| **SerpApi Live Calls** | 60+ calls (~118 credits) | **8 calls** | **86.7% reduction** |
| **Candidates Checked** | 60+ candidates | **2 candidates** (Goa, Ooty) | **Bounded at cap (<= 6)** |
| **Early Exit Reason** | None (Exhausted all 60+) | **FIRST_FEASIBLE** (after Ooty) | **Immediate halt** |
| **Destination Outcome** | NOT_FEASIBLE (No corridor rescue) | **FEASIBLE (Ooty)** | **Feasible within ₹15,000** |

### Live Provider Calls Dispatched (Total = 8)
1. `google_hotels`: `Hotels in Goa`, check-in 2026-11-03, 3 adults (Candidate 1: Goa)
2. `google_maps_directions`: `Chennai -> Goa` (Candidate 1: Goa)
   *(Flight call skipped for Goa because Chennai→Goa has an offline train corridor)*
   *(Goa evaluated: hotel + food + transit exceeded budget)*
3. `google_hotels`: `Hotels in Ooty`, check-in 2026-11-03, 3 adults (Candidate 2: Ooty)
4. `google_maps_directions`: `Chennai -> Ooty` (Candidate 2: Ooty)
   *(Flight call skipped for Ooty because Chennai→Ooty has an offline train corridor)*
   *(Ooty evaluated: Nilgiri Express corridor ₹1,680 + real hotel ₹5,694; optimizer adjusted duration to 1 day; FEASIBLE! Early exit triggered!)*
5. `google_maps`: `places attractions in Ooty` (Post-feasibility attraction enrichment)
6. `google_maps`: `local food in Ooty` (Post-feasibility attraction enrichment)
7. `google_maps`: `food in Ooty` (Post-feasibility attraction enrichment)
8. `google_maps`: `theme parks in Ooty` (Post-feasibility attraction enrichment)

### Selected Plan Details
- **Selected Destination:** **Ooty**
- **Selected Transport:** Nilgiri Express (to Mettupalayam, then Mountain Railway) — **₹1,680.00** (Verified train corridor, 3 passengers round-trip)
- **Selected Hotel:** Jennings Abode Doddabetta — **₹5,694.00** (Real SerpApi Google Hotels property)
- **Financial Breakdown:**
  - Total Budget: ₹15,000.00
  - Fixed Costs (Travel + Stay): ₹7,374.00
  - Daily Allowance (Food & Local Transit): ₹2,700.00
  - Activities / Discretionary: ₹750.00
  - Rescue Reserve (Bucket D): ₹1,500.00
  - **Total Planned:** **₹12,324.00**
  - **Surplus Remaining:** **₹2,676.00**
- **Budget Optimizations Applied:**
  - Trip duration optimized to 1 day to ensure full budget compliance.
- **Attractions:** Real Google Maps places queried dynamically for local food and theme parks; zero invented landmarks.

---

## 6. Verification Against All Hard Invariants

| Requirement | Observed Live Behavior | Status |
|---|---|---|
| Candidates checked <= 6 | Checked exactly 2 candidates (Goa, Ooty) | ✅ **PASS** |
| Provider calls <= 10 | Dispatched exactly 8 live SerpApi calls | ✅ **PASS** |
| Early exit after first feasible | Halted immediately after Candidate 2 (Ooty) succeeded | ✅ **PASS** |
| No Tokyo / international city | Never reached Tokyo; only domestic candidates checked | ✅ **PASS** |
| No Indian Railways to international city | Zero international trains generated | ✅ **PASS** |
| No ₹0 flight or transport | Nilgiri Express fare = ₹1,680.00 > 0 | ✅ **PASS** |
| No free missing hotel | Real hotel Jennings Abode Doddabetta = ₹5,694.00 > 0 | ✅ **PASS** |
| No fabricated provider/hotel | Real hotel extracted directly from Google Hotels provider payload | ✅ **PASS** |
| Correct round-trip × people | Train fare properly multiplied across 3 travelers round-trip | ✅ **PASS** |
| Valid corridor transport for ground destinations | Ooty used Nilgiri Express corridor from train catalog | ✅ **PASS** |
| Zero quota leakage during regression | All 636 tests ran with `SERPAPI_LIVE_ENABLED=false` | ✅ **PASS** |

---

## 7. Status & Readiness Assessment

- **Feasibility Gate:** Fully verified and operating deterministically.
- **Quota Safety:** Bounded by configuration (`MAX_LIVE_CANDIDATES_PER_REQUEST = 6`, `MAX_LIVE_PROVIDER_CALLS_PER_REQUEST = 10`), verified by live instrumentation.
- **Current State:** The architecture correctly balances offline corridor rescue and bounded live provider discovery without quota runaway or hallucinated trips.
- **Production Status:** The system is stabilized for hackathon evaluation and demonstration. It is **not** declared production-ready, as external API rate limits (e.g. OpenRouter free-tier quotas) and further edge-case corridor coverage remain subject to ongoing operational hardening.
