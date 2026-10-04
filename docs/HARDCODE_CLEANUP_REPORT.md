# Budlance Hardcoded Travel Data Cleanup Report

## Executive Summary

Following successful live SerpApi runtime verification across Google Travel Explore, Google Flights, Google Hotels, and Google Maps / Local, a comprehensive cleanup of runtime hardcoded travel and provider data was executed.

All runtime fabrication of travel entities (fake airlines, fake flight numbers, synthetic fares, fictional hotels, static destination lists, and cross-engine fallbacks) has been permanently removed from production business logic. Offline datasets (train corridors, bus corridors, rate tables, Gujarat attraction catalog, and parser entity recognition knowledge) have been isolated as non-authoritative fallback and planning heuristic sources with explicit data provenance tracking (`LIVE`, `CACHED`, `FALLBACK`, `ESTIMATED`).

---

## 1. Removed

The following hardcoded data and fabrication routines have been completely eradicated from runtime production logic:

1. **Fake Flight Entities**
   - Removed runtime fabrication of `IndiGo`, flight number `6E-101`, and synthetic fares (₹4,000, ₹8,500) from `src/budlance/orchestrator/orchestrator.py`.
   - FlightOption models are now instantiated **exclusively** by `src/budlance/normalization/flights.py` from verified live or cached Google Flights envelopes.
   - When no flights are returned by the API or cache, the orchestrator returns an empty list (`NO_OPTIONS`), allowing the reverse-budget engine or user to handle the unavailable state without inventing fake flight options.
   - Preserved round-trip costing, traveler scaling (`adults=people`), and deep-link generation. No PNR, seat, or coach fabrication exists.

2. **Fake Hotel Entities**
   - Removed runtime fabrication of fictional hotels (`Heritage Palace`, `Comfort Inn`, `Backpacker Lodge`) and synthetic rates (₹3,000/night, ₹1,800/night, ₹800/night) from `src/budlance/orchestrator/orchestrator.py`.
   - HotelOption models are now instantiated **exclusively** by `src/budlance/normalization/hotels.py` from verified live or cached Google Hotels envelopes.
   - When hotel lookups return empty, `primary_hotel` is set to `None` and `hotel_candidates` is empty (`NO_OPTIONS`). No fabricated hotel names or prices are ever injected.

3. **Static Destination Recommendation Catalog**
   - Removed the runtime static recommendation list `["Goa", "Jaipur", "Udaipur", "Kerala", "Ooty", "Coorg", "Manali"]` from `_discover_destinations` in `src/budlance/orchestrator/orchestrator.py`.
   - Open-ended destination discovery now relies solely on live Google Travel Explore (`google_travel_explore`).
   - If Travel Explore yields no viable candidates, the orchestrator returns a controlled `CLARIFICATION` response (`"I couldn't find available destinations matching your budget from {origin}. Where would you like to travel...?"`) while cleanly preserving user constraints. Static destinations are never silently substituted.

4. **Cross-Engine Fallback Contamination**
   - Eliminated any cross-pollination between transport modes: a failure in Google Flights never activates train corridor data as a "flight" option.
   - Flight failures return empty flight options; train requests query train corridors or rail fallbacks independently.
   - Hotel failures never fabricate placeholder properties.

5. **Inline Unsupported Travel Pricing**
   - Removed inline hardcoded train fare dictionaries (`{"1ac": 4850, "2ac": 2850, "3ac": 1950, "sleeper": 750, "default": 1500}`) previously embedded in orchestrator business logic.
   - Relocated unmapped rail fare estimates to `data/fallback/rate_tables.json` under `offline_train_fare_estimates_inr`, accessed via `FallbackDataProvider.get_offline_train_fare_estimate()`.

---

## 2. Isolated Fallbacks

Legitimate offline datasets have been quarantined and strictly classified:

1. **Train Corridor Data (`data/fallback/train_corridors.json`)**
   - *Classification:* `DataSource.FALLBACK` / `OFFLINE`.
   - *Scope:* Used strictly for train transit planning when live railway booking engines are absent.
   - *Isolation:* Envelopes carry `source=DataSource.FALLBACK` and `is_fallback=True`. Never mislabeled as live availability; never cross-pollinated into flights or hotels.

2. **Bus Corridor Data (`data/fallback/bus_corridors.json`)**
   - *Classification:* `DataSource.FALLBACK` / `OFFLINE`.
   - *Scope:* Used only when bus transit is explicitly requested or evaluated as road transit.
   - *Isolation:* Explicitly marked as fallback; completely separated from air and lodging pipelines.

3. **Gujarat Attractions Catalog (`data/attractions/gujarat.json`)**
   - *Classification:* Curated offline fallback.
   - *Precedence:* In `src/budlance/itinerary/generator.py`, live Google Maps / local place discovery (`PlaceOption` with `is_fallback=False` and `source="LIVE"`) takes absolute precedence.
   - *Isolation:* The static Gujarat catalog activates **only** when live place discovery returns no results or is unconfigured. Live places never get overwritten by the static catalog.

4. **Deterministic Parser City Knowledge (`src/budlance/ai/service.py`)**
   - *Classification:* `FALLBACK_PARSER_KNOWLEDGE` (`FALLBACK_PARSER_CITIES`).
   - *Scope:* Static tuple of 26 city names (`kerala`, `goa`, `ooty`, `manali`, etc.) used strictly for regex tokenization and entity extraction in the offline heuristic parser (`_extract_destination`, `_parse_offline`) when LLMs (OpenRouter/Gemini) are unavailable.
   - *Isolation:* Strictly isolated from travel discovery, pricing, and provider APIs. Never supplies pricing, fares, or destination recommendations.

---

## 3. Configuration

Planning heuristics and internal service fees are managed via configuration and rate tables:

1. **Food Rates (`src/budlance/config.py` & `data/fallback/rate_tables.json`)**
   - `Settings.food_budget_rates`: `{"budget": 400, "standard": 800, "comfort": 1500}` INR/day.
   - Maintained as internal planning heuristics, never represented as external live market quotes.

2. **Transit Rates (`src/budlance/config.py` & `data/fallback/rate_tables.json`)**
   - `Settings.transit_rates`: `{"auto_per_km": 15, "cab_per_km": 22, "metro_bus_daily_pass": 100}` INR.
   - Maintained as internal planning heuristics for local mobility estimations.

3. **Rescue Reserve (`src/budlance/config.py` & `data/fallback/rate_tables.json`)**
   - `Settings.rescue_reserve_percent`: `Decimal("0.10")` (10% safety cushion for Bucket D).

4. **Trip Pass Price (`src/budlance/config.py` & `src/budlance/payment/service.py`)**
   - `Settings.trip_pass_amount`: `Decimal("49.00")` (`TRIP_PASS_AMOUNT`).
   - `DEFAULT_PASS_FEE_INR`: `Decimal("49.00")`.
   - Kept strictly as Budlance's own service fee; isolated from Bucket A/B/C/D travel expenditures.

---

## 4. Legitimate Constants

The following constants remain in the codebase and are verified legitimate:

| Constant | Location | Purpose |
| :--- | :--- | :--- |
| `PLANNING`, `ACTIVE`, `COMPLETED`, `CANCELLED` | `src/budlance/db/models.py` | Trip lifecycle status enum values |
| `TripAction` enum values | `src/budlance/ai/schemas.py` | Intent classification targets (`NEW_TRIP`, `CHANGE_BUDGET`, `RESCUE`, etc.) |
| Bucket A/B/C/D Identifiers | `src/budlance/engine/models.py` | Core reverse-budget waterfall partition buckets |
| Engine Names | `src/budlance/serpapi/` | SerpApi engine identifiers (`google_travel_explore`, `google_flights`, `google_hotels`, `google_maps`) |
| API Base URL | `src/budlance/serpapi/gateway.py` | SerpApi endpoint (`https://serpapi.com/search.json`) |
| IRCTC Search URL | `src/budlance/orchestrator/orchestrator.py` | Generic deep-link template (`https://www.irctc.co.in/nget/train-search`) |
| Google Flights URL Builder | `src/budlance/normalization/flights.py` | Generic URL template dynamically parameterizing origin/destination |
| Telegram Commands | `src/budlance/bot/handlers.py` | Bot slash command handlers (`/start`, `/trip_pass`, `/demo_pass`, `/help`) |
| HTTP Status Codes & Error Classes | `src/budlance/serpapi/exceptions.py` | Protocol error boundary definitions |

---

## 5. Runtime Authority

| Domain | Authoritative Source | Fallback / Offline Source |
| :--- | :--- | :--- |
| **Flights** | **Live SerpApi Google Flights** (`google_flights`) | None (Controlled `NO_OPTIONS` / empty list) |
| **Hotels** | **Live SerpApi Google Hotels** (`google_hotels`) | None (Controlled `NO_OPTIONS` / empty list) |
| **Destinations** | **Live SerpApi Travel Explore** (`google_travel_explore`) | None (Controlled `CLARIFICATION` prompt) |
| **Places / Attractions** | **Live SerpApi Google Maps** (`google_maps`) | Curated Gujarat catalog (`data/attractions/gujarat.json`) |
| **Trains** | Static regional corridors (`data/fallback/train_corridors.json`) | Unmapped rate table estimates (`offline_train_fare_estimates_inr`) |
| **Buses** | Static regional corridors (`data/fallback/bus_corridors.json`) | None |
| **Food** | Budlance Planning Heuristics (`rate_tables.json`) | Config defaults (`Settings.food_budget_rates`) |
| **Local Transport** | Budlance Planning Heuristics (`rate_tables.json`) | Config defaults (`Settings.transit_rates`) |

---

## 6. Production Safety Question

> **"When live APIs return valid data, can hardcoded provider/travel data override or replace the live result?"**

### Answer:
# **NO**

Live SerpApi results are normalized and selected with absolute precedence. Hardcoded provider/travel data never overrides, replaces, or competes with valid live results.

---

## 7. Remaining Hardcoded Data Audit

| File | Line | Content / Identifier | Purpose | Runtime Reachable? | Why It Remains |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `src/budlance/ai/service.py` | 34–39 | `FALLBACK_PARSER_CITIES: tuple[str, ...]` | Entity extraction dictionary (26 city names) | Yes (Heuristic parser only) | Deterministic parsing when LLM is unavailable; never supplies pricing or recommendations. |
| `src/budlance/serpapi/location.py` | 30–91 | `_CITY_TO_IATA: dict[str, str]` | City-to-IATA airport code dictionary | Yes (Flight query parameter resolution) | Required to supply valid `departure_id`/`arrival_id` to SerpApi Google Flights. |
| `src/budlance/cache/fallback.py` | 80–91 | `get_offline_train_fare_estimate()` | Offline rail fare fallback access | Yes (Train search fallback only) | SerpApi does not support IRCTC live ticketing; provides deterministic offline estimates. |
| `data/fallback/train_corridors.json` | 1–86 | Static train corridors | Regional train timetable & fare dataset | Yes (Train engine fallback only) | Required for Indian Railways route planning in absence of live IRCTC API. |
| `data/fallback/bus_corridors.json` | 1–80 | Static bus corridors | Regional intercity bus dataset | Yes (Bus engine fallback only) | Required for regional bus route planning in absence of live bus booking API. |
| `data/fallback/rate_tables.json` | 1–21 | Heuristic cost tables | Food allowances, transit rates, rail estimates | Yes (Estimation layer only) | Reverse-budget planning heuristics; clearly distinguished as `ESTIMATED`. |
| `data/attractions/gujarat.json` | 1–150 | Curated Gujarat attractions | Curated historical sites & entry fees | Yes (Attraction fallback only) | Offline fallback for Gujarat when Google Maps places API is unconfigured or returns empty. |
| `src/budlance/normalization/flights.py` | 54 | `https://www.google.com/travel/flights` | Generic flight search link | Yes (Fallback link only) | Safe generic URL when origin/destination endpoint strings are invalid or missing. |
| `src/budlance/orchestrator/orchestrator.py` | 490 | `https://www.irctc.co.in/nget/train-search` | IRCTC train booking search URL | Yes (User booking link) | Authoritative public Indian Railways portal for user self-checkout. |
| `src/budlance/payment/service.py` | 30 | `DEFAULT_PASS_FEE_INR = Decimal("49.00")` | Budlance Trip Pass fee | Yes (Payment handoff) | Budlance's own service monetization fee; not a travel provider price. |

---

## 8. Verification Results

### Unit Test Suite (Scenarios A through G)
- **Scenario A (Live Flight Precedence):** Live IndiGo result (6E-555, ₹5,200) utilized; fake 6E-101 / ₹4,000 NOT created. **PASSED**
- **Scenario B (No Flights Available):** Empty API response produces empty flight options list; NO fake flight created. **PASSED**
- **Scenario C (Live Hotel Precedence):** Real hotel (The Taj Mahal Palace, ₹12,500) normalized and selected; fake Heritage Palace / Comfort Inn NOT created. **PASSED**
- **Scenario D (No Hotels Available):** Empty API response produces `primary_hotel = None` and empty candidate list; NO fake hotel created. **PASSED**
- **Scenario E (Destination Discovery):** Live Travel Explore candidates used; static catalog NOT appended. Empty explore query yields empty candidate list without inventing destinations. **PASSED**
- **Scenario F (Attraction Precedence):** Live Google Maps places take precedence over static Gujarat catalog in itinerary generation. **PASSED**
- **Scenario G (Cross-Engine Isolation):** Google Flights API failure yields empty flight options; train options do NOT leak into flight options. **PASSED**
- **Rail Fallback Isolation:** Offline train fare estimates properly resolved from `rate_tables.json` rather than inline code literals. **PASSED**
- **Parser Knowledge Isolation:** `FALLBACK_PARSER_CITIES` extracts entities in offline mode without supplying pricing or destination discovery recommendations. **PASSED**
- **Planning Heuristics & Trip Pass:** Food, local transit, rescue reserve, and Trip Pass (₹49) validated as configuration items. **PASSED**

### Full Regression Suite
- **268 Tests Passed** across `tests/test_hardcode_cleanup.py`, `tests/test_serpapi_guard.py`, `tests/test_serpapi_param_contract.py`, `tests/test_serpapi_and_cache.py`, `tests/test_action_router.py`, `tests/test_conversational_intent.py`, `tests/test_attraction_discovery_ordering.py`, `tests/test_attraction_selector.py`, `tests/test_orchestrator_pipeline.py`, `tests/test_round_trip_transport.py`, `tests/test_transport_preference.py`, `tests/test_telegram_e2e_phase7.py`, and `tests/test_trip_pass_and_serpapi_phase8.py`.
- **70 Engine & Lifecycle Tests Passed** across `tests/test_budget_and_optimizer.py`, `tests/test_reoptimizer.py`, `tests/test_rescue_mode.py`, `tests/test_expense_lifecycle.py`, and `tests/test_normalization_and_estimation.py`.
- **Zero SerpApi live searches consumed** during regression test execution.
