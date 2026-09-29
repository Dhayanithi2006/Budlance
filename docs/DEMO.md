# Budlance — Hackathon Demo Guide

> Demo category: **Travel & Local Discovery**  
> Core innovation: **Reverse-Budget Travel Planning** — the user's budget is the constraint,
> not an afterthought.

---

## Part 1 — Primary Demo Scenario

### Setup (before demo)

1. Application running locally or on a deployment:
   ```bash
   uv run uvicorn budlance.api.app:app --host 0.0.0.0 --port 8000
   ```
2. Telegram webhook registered (or ngrok tunnel active).
3. `.env` contains a valid `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, and `SERPAPI_API_KEY`.
4. Verify `/health`:
   ```bash
   curl http://localhost:8000/health
   # → {"status":"healthy","telegram_configured":true,...}
   ```

### Demo message (send from Telegram)

```
₹15,000, 5 days, 3 people, beaches and nature
```

*(Alternatively: "Plan a trip from Chennai for 3 people, 5 days, with budget ₹15,000, beaches and nature")*

### What Budlance does (step by step)

| Step | System component | Data source |
|------|-----------------|-------------|
| Parse intent | AI Intent Layer (OpenRouter) | — |
| Discover destination | google_travel_explore | `LIVE` or `CACHED` |
| Fetch flights | google_flights | `LIVE` / `CACHED` / `FALLBACK` |
| Fetch hotels | google_hotels | `LIVE` / `CACHED` / `FALLBACK` |
| Fetch places | google_maps | `LIVE` / `CACHED` / `FALLBACK` |
| Fetch route | google_maps_directions | `LIVE` / `CACHED` / `FALLBACK` |
| Estimate food cost | Estimation Layer | `ESTIMATED` |
| Estimate local transit | Estimation Layer | `ESTIMATED` |
| Feasibility check | Reverse-Budget Engine | — |
| If over budget → optimize | Optimization Engine (max 4 attempts) | — |
| Generate itinerary | Itinerary Generator | — |
| Initialize ledger | Virtual Ledger Manager | — |
| Persist | Supabase PostgreSQL | — |
| Send response | Telegram | — |

### Expected Telegram response (sections)

```
🌴 Budlance Trip Plan: Goa
👥 3 travelers | ⏱️ 5 days

💰 Financial Waterfall:
• Total Budget: INR 15,000.00
• Fixed Costs (Travel + Stay): INR X,XXX.XX
• Daily Allowance (Food & Local Transit): INR X,XXX.XX
• Activities / Discretionary: INR X,XXX.XX
• Rescue Reserve (Bucket D): INR X,XXX.XX
• Total Planned: INR X,XXX.XX
• Surplus Remaining: INR X,XXX.XX

🧳 Selected Bookings:
• Transport: IndiGo — INR X,XXX.XX [LIVE]
• Accommodation: Goa Bay Resort — INR X,XXX.XX [LIVE]

📅 Day-by-Day Schedule:
Day 1: Arrival & Beach  ...
Day 2: ...
...
```

**Highlight to the audience:**
- `[LIVE]` tags confirm SerpApi data was used.
- `[ESTIMATED]` tags show heuristic costs are clearly separated.
- Financial Waterfall shows the exact budget allocation.
- If the Optimizer ran, "⚡ Budget Optimizations Applied" section appears.

---

## Part 2 — Over-Budget + Optimization Demo

### Demo message

```
Plan a trip from Mumbai to Goa for 2 people, 3 days, with budget ₹14,000
```

*(This budget is tight enough to trigger the optimizer for the default hotel tier.)*

### What to highlight

- The optimizer downgrades from a 4-star hotel to a 3-star option.
- The response shows "⚡ Budget Optimizations Applied: Hotel tier downgrade (4→3 star)".
- Final allocation still ≤ ₹14,000.
- The Reverse-Budget Engine — not the AI — made the feasibility decision.

---

## Part 3 — Rescue Scenario A: Weather Disruption

**Prerequisite:** A FEASIBLE trip must have been planned in the same Telegram chat.

### Demo message

```
It's raining heavily at the beach
```

### What Budlance does

1. Loads the active trip and itinerary from Supabase.
2. Parses rescue intent (weather_closure).
3. Calls `google_maps` for indoor alternatives in the trip destination.
4. Runs mini feasibility via the Reverse-Budget Engine.
5. Updates itinerary and ledger if feasible.

### Expected Telegram response

```
🌦️ Rescue Mode: Alternative Found!

Due to: heavy rain at beach
✅ Selected Alternative: Museum of Christian Art [LIVE]
💰 Budget Impact: +₹0.00 (Covered by Rescue Reserve)

📝 Updated Itinerary Saved: The affected activity has been replaced and your ledger remains balanced.
```

---

## Part 4 — Rescue Scenario B: Price Dispute

**Prerequisite:** A FEASIBLE trip must have been planned in the same Telegram chat.

### Demo message

```
The auto driver is asking ₹600
```

### What Budlance does

1. Loads the active trip.
2. Parses rescue intent (price_dispute).
3. Calculates fair fare using **local rate tables** (no SerpApi call).
4. Records `₹600` as `USER_REPORTED` spending in the Virtual Ledger.

### Expected Telegram response

```
🚕 Advisory Transit Fare Guidance:

• Quoted Price: ₹600.00 [USER_REPORTED]
• Estimated Fair Fare: ₹X.XX [ESTIMATED] (₹X/km for ~10 km)
• Status: SIGNIFICANTLY HIGH

ℹ️ Advisory fare guidance based on standard auto rate tables...

📝 Recorded reported expenditure in your Virtual Ledger.
```

**Highlight:** `[USER_REPORTED]` vs `[ESTIMATED]` — Budlance always separates declared
spending from its own estimates.

---

## Part 5 — NOT_FEASIBLE Scenario (optional)

### Demo message

```
Plan a trip from Mumbai to Goa for 2 people, 5 days, with budget ₹2,000
```

### Expected Telegram response

```
❌ Trip Plan Not Feasible within Budget

I tried to build a trip to Goa within your budget of INR 2,000.00...

• Deficit: INR X,XXX.XX
• Details: ...

💡 Recommendation: Consider increasing your budget by at least INR X,XXX.XX or reducing the trip duration by 1 day.
```

**Highlight:** Budlance never silently fails. It tells the user exactly how much more they need.

---

## Part 6 — Data Source Transparency Check

Show the audience the provenance tags in the response:

| Tag | What it means |
|-----|--------------|
| `[LIVE]` | Price/data fetched from SerpApi in this request |
| `[CACHED]` | Reused from a previous SerpApi call |
| `[FALLBACK]` | Came from static data (train/bus JSON) |
| `[ESTIMATED]` | Heuristic (food, local auto) |
| `[USER_REPORTED]` | Stated by the user during the live trip |

---

## Three-Minute Demo Script

> **Suitable for hackathon video / live presentation**

---

**[0:00 – 0:20] The problem**

> "Most travel apps ask: where do you want to go? Then they show you options and leave
> you to figure out if you can afford it.
>
> Budlance flips this. You start with your budget. Budlance determines what trip you can
> actually afford."

---

**[0:20 – 0:50] Send the first message**

> Open Telegram. Send:
> *"₹15,000, 5 days, 3 people, beaches and nature"*

> "Budlance extracts structured intent using OpenRouter. Then it calls SerpApi — live
> Google Flights, Google Hotels, Google Maps — to get real prices."

---

**[0:50 – 1:30] Show the response**

> Scroll through the Telegram response with the audience.

> "Here's the Financial Waterfall — this is Budlance's core. Every rupee is accounted for:
> fixed costs like flights and hotel, daily survival budget for food and local transit, a
> discretionary pool for activities, and a rescue reserve."

> Point to the `[LIVE]` tags: *"These prices came from SerpApi — they affected whether
> this trip was feasible at all."*

---

**[1:30 – 1:50] Show the optimizer (if triggered)**

> "If the initial plan didn't fit, Budlance runs a four-step optimizer — hotel downgrade,
> transport downgrade, shorten the trip, trim discretionary. It does this automatically.
> You can see what changed here."

---

**[1:50 – 2:15] Rescue Mode**

> Still in the same chat, send: *"It's raining heavily at the beach"*

> "The user is on the trip. It's raining. Budlance detects this is a rescue request, loads
> the active trip, searches Maps for indoor alternatives, checks if the replacement fits the
> rescue reserve, and updates the itinerary. No replanning from scratch — it modifies the
> existing plan."

---

**[2:15 – 2:40] Price dispute**

> Send: *"The auto driver is asking ₹600"*

> "Now a fair-price check. Budlance doesn't call SerpApi for this — it uses rate tables.
> It compares the quoted price against a standard estimate and records ₹600 as the user's
> reported spending in the Virtual Ledger. The ledger is not a bank account — it's a
> budget tracker that survives across Telegram conversations."

---

**[2:40 – 3:00] Close**

> "Budlance is a complete reverse-budget planning system. Every cost is labeled — live,
> estimated, fallback, or user-reported. The budget engine is authoritative. The AI
> understands intent but never makes financial decisions. And the entire system runs
> through Telegram — no separate app needed."

---

## Technical Notes for Judges

- **119 automated tests** covering the full pipeline: budget engine, optimizer,
  itinerary, ledger, rescue service, and hardening against API/persistence failures.
- **SerpApi prices materially affect feasibility** — verified by `test_serpapi_material_contribution`.
- **No hardcoded secrets** — all credentials from `.env`.
- **Offline/degraded mode** — application starts and tests pass without live API credentials.
- **Fallback transparency** — static data is always labeled `[FALLBACK]`, never presented
  as live SerpApi data.
- **Docker-ready** — single `docker compose up` for local execution.
