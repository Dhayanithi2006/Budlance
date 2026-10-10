# Live Travel & Provider Data Migration Report
**Budlance Neuro-Symbolic Travel Planner — Flight-Focused Final Submission**

---

## 1. Executive Summary

This report documents the comprehensive migration of **Budlance** from hardcoded/curated production travel facts to **live API data sources** (SerpApi Google Flights, Google Hotels, Google Maps/Local, Google Search Events, and Google Travel Explore) while strictly preserving Budlance's core neuro-symbolic architecture, budget waterfall calculation, optimizer sequence, Telegram conversational flows, and offline test guarantees.

### Key Tenets Maintained:
1. **Data-Source Replacement Only:** No architectural redesigns, no Telegram bot modifications, no changes to the mathematical budget waterfall, and zero external services introduced.
2. **Zero Live Fabrication:** When live mode is active (`SERPAPI_LIVE_ENABLED=true`), zero synthetic flights (e.g. `IndiGo 6E-101`), zero fabricated hotels (e.g. `Heritage Palace`), zero synthetic admission fees, zero unconfirmed seasonal events, and zero hardcoded candidate destinations enter live recommendation pipelines.
3. **Preserved Offline Test Boundaries:** 100% of automated tests execute and pass offline with `SERPAPI_LIVE_ENABLED=false` without requiring network access. Offline train/bus corridors and static IATA technical lookup dictionaries remain completely intact as designated offline fallbacks and parser lookup aids.

---

## 2. Comprehensive Migration Matrix

| Component | Prior Hardcoded / Curated State | Live Engine / API Replacement | Offline Gate / Fallback Contract | Live Mode Invariant |
|:---|:---|:---|:---|:---|
| **Destination Discovery** | Hardcoded `_CURATED_DOMESTIC_POOL` (10 cities with tags & budgets) | SerpApi `google_travel_explore` (`q=Explore from {origin}`) | Strictly gated to `if not self.is_live_mode:`. Provides test fixtures when offline. | Zero hardcoded candidates injected; empty live discovery produces empty candidates with transparent user messaging. |
| **Flight Details & Pricing** | Synthetic flight defaults (`IndiGo 6E-101`, fixed ₹4,500/₹7,200 fares) | SerpApi `google_flights` (`departure_id`, `arrival_id`, `outbound_date`) | None in live mode. Offline tests use cached envelopes. | Authentic airlines, flight numbers, cabin classes, and live market quotes. |
| **Hotel Discovery & Pricing** | Synthetic hotel defaults (`Heritage Palace`, fixed rates) | SerpApi `google_hotels` (`q=Hotels in {destination}`) with stay nights calculation | Missing `total_rate` derives total as $\text{price\_per\_night} \times \text{nights}$. | Real property names, star ratings, amenities, and total stay pricing. |
| **Attractions & Sightseeing** | Hardcoded attraction catalog with estimated entry fees | SerpApi `google_maps` / local search via `resolve_places_query` + `AttractionSelector` | If admission fee is unconfirmed by live provider, marked `entry_fee_inr=None, is_fee_unknown=True`. | Never formatted as ₹0. Formatted with `[admission not included]` or omitted tag. |
| **Food & Dining** | Generic meal calculations without local query support | `resolve_food_query(city, interest)` with SerpApi Google Maps local search | Falls back to daily food rate heuristic table if live query returns empty. | Discovers authentic local eateries without fabricating menu prices. |
| **Seasonal Events** | Non-existent or static placeholder assumptions | SerpApi Google Search (`engine="google"`, `q="events in {destination}"`) | `normalize_events` normalizes `events_results`. If empty, returns `[]`. | Zero fabricated events. No deprecated `google_events` engine calls. |
| **Domestic Rail & Bus** | Static corridors in `FallbackDataProvider` | Explicitly preserved as offline transport corridors | Retained for ground travel when user explicitly requests train or in offline sandbox. | Live flight mode never falls back to rail for unsupported flight destinations. |
| **IATA & City Lookups** | `_CITY_TO_IATA` dictionary (50+ cities) | Retained as technical translation layer for SerpApi parameters | Internal lookup utility only; not authoritative travel inventory. | Translates user city names to 3-letter IATA codes for SerpApi queries. |

---

## 3. Detailed Phase Breakdown & Implementation Verification

### Phase 1: Comprehensive Codebase Audit
- Inspected all call sites of `_CURATED_DOMESTIC_POOL`, `_CITY_TO_IATA`, `normalize_hotels`, `AttractionSelector`, `ItineraryGenerator`, and `BudlanceOrchestrator`.
- Categorized all occurrences into:
  - **Category F (Forbidden hardcoded facts):** Live path synthetic flights, hotels, rates, admission fees.
  - **Category A (Allowed offline fallbacks):** Train/bus corridors in `FallbackDataProvider`.
  - **Category T (Technical lookup metadata):** `_CITY_TO_IATA` mappings.
  - **Category P (Planning heuristic rates):** Food/transit daily allowances in `budlance.config`.

### Phase 2 & 10: Live Destination Discovery & Curated Pool Boundary Gating
- Located in `src/budlance/orchestrator/orchestrator.py` (`_discover_destinations`).
- `_CURATED_DOMESTIC_POOL` is strictly gated behind `if not self.is_live_mode:`.
- In live mode (`SERPAPI_LIVE_ENABLED=true`), zero curated domestic items enter the candidate pool. Candidate discovery relies entirely on live `google_travel_explore` results.
- In offline mode (`SERPAPI_LIVE_ENABLED=false`), `_CURATED_DOMESTIC_POOL` supplies offline test candidates to ensure all lifecycle regression tests pass seamlessly.

### Phase 3: Live Flight Detail Retrieval & Deep Links
- Live flights from `google_flights` normalize via `normalize_flights` in `src/budlance/normalization/flights.py`.
- Preserves airline name, flight number, departure/arrival timestamps, stops, travel class, and booking deep links.
- When live flights return empty results, the system returns `[]` without fabricating fake options.

### Phase 4: Live Hotel Discovery & Stay Duration Pricing Semantics
- Updated `src/budlance/normalization/hotels.py` and `src/budlance/normalization/normalizer.py`:
  - Added `nights: int = 1` parameter to `normalize_hotels`.
  - When `total_rate` is missing from SerpApi but `rate_per_night` is available, calculates `total_price = rate_per_night * nights` rather than using a single night's rate.
  - Updated all call sites in `src/budlance/orchestrator/orchestrator.py` (lines 591, 1876, 2097) to pass `stay_nights = max(1, days - 1)`.

### Phase 5: Live Attractions, Places & Unknown Entry Fee Semantics
- Updated `src/budlance/attractions/models.py` and `src/budlance/itinerary/models.py`:
  - Changed `entry_fee_inr: int | None = None`.
  - Added `is_fee_unknown: bool = False`.
- Updated `src/budlance/attractions/selector.py`:
  - `select_for_itinerary` accepts `places: list[Any] | None = None`. Converts live provider `PlaceOption` objects into `Attraction` objects, marks fees as unknown (`is_fee_unknown=True, entry_fee_inr=None`), and filters them through party compatibility, interest matching, and time-of-day diversity.
- Updated `src/budlance/orchestrator/formatter.py`:
  - Verified that unknown fees are never displayed as `₹0`. Formats as `[admission not included]` or omits fee tags.

### Phase 6: Live Food & Dining Discovery
- Added `resolve_food_query(city: str, interest: str | None = None) -> str` in `src/budlance/serpapi/location.py`.
- Wires food queries to SerpApi Google Maps local search (`engine="google_maps"`, `type="search"`).
- When live food results are empty, the orchestrator safely defaults to the configurable daily food rate heuristic table (`config.food_budget_rates`) without inventing specific restaurant items or prices.

### Phase 7: Live Seasonal Events Discovery
- Created `EventOption` schema in `src/budlance/schemas/travel.py`.
- Created `normalize_events` in `src/budlance/normalization/events.py` targeting SerpApi Google Search (`engine="google"`, `q=f"events in {destination}"`, parsing `events_results`).
- Verified zero usage of the deprecated `google_events` engine.
- Wires event discovery into `src/budlance/orchestrator/orchestrator.py`. If `events_results` is empty or unavailable, returns an empty list without fabricating fictitious local festivals.

### Phase 8 & 9: Budget Waterfall & Re-Optimization Integrity
- Preserved all mathematical budget equations in `ReverseBudgetEngine` and `BudgetOptimizer`:
  - Bucket A (Fixed): Transport + Accommodation.
  - Bucket B (Survival): Food + Local Transit.
  - Bucket C (Activities): Attractions & Discretionary.
  - Bucket D (Rescue): Emergency Reserve ($10\%$).
- Optimizer sequence (Hotel downgrade $\to$ Transport downgrade $\to$ Duration reduction) remains completely untouched.

### Phase 11, 12, & 13: Scope Boundaries
- **Train/Bus Corridors (Category A):** Corridors in `FallbackDataProvider` remain untouched for ground transit queries and offline testing.
- **Static City List (Category T):** Technical lookup dictionary for SerpApi IATA codes (`_CITY_TO_IATA`) remains untouched.
- **Flight Fallback Safeguard (Phase 14):** In live mode, unsupported flight destinations do not silently fallback to domestic rail unless the traveler explicitly asked for train travel.

### Phase 15: Telegram Bot Live Display Integrity
- Formatted messages cleanly convey live flight airlines, flight numbers, hotel ratings, and itinerary items.
- Attractions with unknown entry fees display with transparent admission disclaimers.

### Phase 16: Automated Regression Test Suite
- Created `tests/test_live_provider_migration.py` with 11 targeted tests:
  1. `test_live_mode_excludes_curated_domestic_pool`: PASS
  2. `test_offline_mode_preserves_curated_domestic_pool`: PASS
  3. `test_hotel_normalization_multiplies_rate_by_nights`: PASS
  4. `test_hotel_normalization_uses_total_rate_when_present`: PASS
  5. `test_attraction_selector_converts_live_places_and_preserves_unknown_fee`: PASS
  6. `test_formatter_does_not_display_unknown_fee_as_zero`: PASS
  7. `test_normalize_events_parses_serpapi_events_results`: PASS
  8. `test_normalize_events_empty_results_does_not_fabricate`: PASS
  9. `test_live_mode_forbids_rail_fallback_for_unsupported_flight_route`: PASS
  10. `test_offline_mode_permits_rail_fallback_for_tests`: PASS
  11. `test_resolve_food_and_event_queries`: PASS

### Phase 17: Full Automated Test Suite Execution
- Executed complete test suite across all modules with `SERPAPI_LIVE_ENABLED=false`:
  - **Total Tests:** 683
  - **Passed:** 683
  - **Failed:** 0
  - **Duration:** 43.04s

---

## 4. Static Audit & Code Cleanliness (Phase 18 & 19)

- **Zero Synthetic Flights:** `src/` contains 0 instances of `IndiGo 6E-101` in production execution paths.
- **Zero Synthetic Hotels:** `src/` contains 0 instances of `Heritage Palace` in production execution paths.
- **No Architectural Drifts:** Git status confirms all edits are strictly limited to normalization, selection, location query formatting, and orchestrator data wiring.

---

## 5. Conclusion & Readiness

The migration of Budlance to live travel data sources is complete, thoroughly verified, and regression-free:
- **Production Live Mode:** Connects to live SerpApi engines with zero data fabrication.
- **Offline / Test Mode:** Preserves deterministic offline fallbacks, maintaining a 100% pass rate across all 683 tests.
