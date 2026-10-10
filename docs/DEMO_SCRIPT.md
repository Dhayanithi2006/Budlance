# 🎬 Budlance Live Hackathon Demo Script

> **Golden Rule for Live Demo**: Do **not** improvise messages. Copy-paste or type these exact verified prompts. Everything runs strictly against cached live data and offline heuristics with zero network latency.

---

## ⏱️ Live Presentation Timeline (Total: 4–5 Minutes)

| Time | Segment | Action / Screen | Key Message |
|------|---------|-----------------|-------------|
| **0:00 - 0:45** | **The Problem & Value Prop** | Title slide / Opening | Most travel planners start with a destination and inflate the bill. Budlance is a **reverse-budget AI travel agent** that starts from what you can afford, plans within bounds, and hands off directly to providers. |
| **0:45 - 1:45** | **Scenario 1: Flight Route & Deep Link** | Telegram Bot | Type Flight prompt (Chennai → Delhi, ₹90k). Show IATA flight resolution, assumed travel dates, real carrier (Air India AI 2886), and the exact Google Flights handoff link. |
| **1:45 - 2:45** | **Scenario 2 & 4: Train Corridor & In-Trip Rescue** | Telegram Bot | Type Chennai → Bangalore train prompt. Clarify class (`3AC`). Reply `Booked`. Unpack the full day-by-day plan, financial waterfall, and event alert. Log an expense and trigger fair auto fare rescue. |
| **2:45 - 3:30** | **Scenario 3: Feasibility Gate** | Telegram Bot | Type Goa ₹10k luxury prompt. Show the bot **refusing to hallucinate a false budget**, computing the exact deficit, and offering viable alternatives. |
| **3:30 - 4:00** | **Scenario 5: Tanglish Local Voice** | Telegram Bot | Type the Tanglish prompt. Show regional conversational support. |
| **4:00 - 4:30** | **Pitch Limits & Roadmap** | Roadmap Slide | State limitations with integrity (IRCTC estimates, simulated payments, prototype status). Pitch roadmap: cosine similarity embeddings for interest matching, Razorpay UPI, IRCTC agent API. |

---

## 📝 Exact Rehearsal Prompts & Expected Responses

### 🛫 Scenario 1: Feasible Flight Route (Chennai → Delhi)
**Prompt to send:**
```text
Plan a trip to Delhi from Chennai for 2 people, 3 days, with budget Rs 90,000, prefer flight
```
**What the Bot Prints:**
- **Status**: Viable Flight Option.
- **Estimated Travel Cost**: ₹45,352.00 (Round Trip for 2).
- **Flight Carrier & Schedule**: Air India `AI 2886`, Dep 08:05 — Arr 11:00.
- **Exact Handoff URL**:
  `https://www.google.com/travel/flights?q=Flights+to+DEL+from+MAA+on+2026-11-08+through+2026-11-10`
- **What to say on stage**:
  > *"Budlance plans and hands off; the user pays the airline directly. Notice zero placeholder text — genuine IATA airport codes (MAA to DEL) and calculated round-trip dates are baked into the deep link."*

---

### 🚆 Scenario 2: Train & Bus Corridor with Class Clarification (Chennai → Bangalore)
**Step 2A — Prompt to send:**
```text
Plan a trip from Chennai to Bangalore for 2 people, 2 days, with budget Rs 25,000, prefer train
```
**Bot Response:**
```text
Which train class would you prefer — Sleeper, 3AC, 2AC or 1AC?
```

**Step 2B — Prompt to send:**
```text
3AC
```
**Bot Response:**
- Broad gauge railhead verified.
- Train: Shatabdi Express (Estimated ₹3,000.00).
- External handoff link to official IRCTC portal: `https://www.irctc.co.in/nget/train-search`.
- Next instruction: `Reply with Booked once you have completed your external booking.`

---

### 🛡️ Scenario 4: Booking Confirmation & In-Trip Rescue
*(Continuing directly from Scenario 2 in the same chat)*

**Step 4A — Unlock Itinerary:**
```text
Booked
```
**Bot Response:**
- Transitions trip to **ACTIVE**.
- Displays full **Financial Waterfall**:
  - Total Budget: ₹25,000
  - Fixed Costs (Stay + Travel): ₹11,605
  - Daily Food & Transit: ₹1,800
  - Rescue Reserve (Bucket D): ₹2,500
  - Surplus: ₹7,345
- Selected Accommodation: Welcomhotel by ITC Hotels, Bengaluru (with real booking URL).
- Day 1 Schedule: Morning at Lalbagh Botanical Garden, Afternoon at Bangalore Palace.
- Local Event Alert: Bangalore Lalbagh Botanical Exhibition!

**Step 4B — Log Expense:**
```text
Spent ₹1200 on dinner at local cafe
```
**Bot Response:**
```text
Recorded ₹1,200 for Day 1. Day 1 is still active.
```

**Step 4C — In-Trip Fare Dispute Rescue:**
```text
The auto driver is asking ₹450 for 5 km, is it fair?
```
**Bot Response:**
- Parses exact distance: `5 km`.
- Computes baseline fare via statutory table (`₹15/km` = `₹75.00`).
- Alerts: `🚕 Status: SIGNIFICANTLY HIGH (Quoted ₹450 vs estimated ₹75.00)`.
- Records warning in Virtual Ledger without halting the trip.
- **What to say on stage**:
  > *"This is In-Trip Rescue. When unexpected friction happens during travel, Budlance guards your pocket with advisory heuristics in real time."*

---

### 🛑 Scenario 3: Reverse-Budget Feasibility Gate (Zero Hallucination)
*(Send in a new chat or reset session)*

**Prompt to send:**
```text
Plan a 5-day luxury trip from Chennai to Goa for 4 people with budget Rs 10,000
```
**Bot Response:**
- **Status**: `❌ Trip Plan Not Feasible within Budget`
- Computes mandatory survival costs (travel + basic lodging).
- Displays exact deficit: `• Deficit: INR 11,800.00`.
- Explains why after 4 optimization downgrade attempts it cannot be completed safely within ₹10,000.
- Recommends increasing budget to ₹21,800 or reducing party/days.
- **What to say on stage**:
  > *"Budlance will never hallucinate a trip that leaves you stranded. If a budget is mathematically infeasible, the engine rejects it upfront and gives actionable guidance."*

---

### 🗣️ Scenario 5: Tanglish / Local Dialect Natural Language
**Prompt to send:**
```text
Chennai la irundhu Pondicherry poganum 2 days budget 15000
```
**Bot Response:**
```text
Got it — Chennai → Pondicherry, ₹15,000, 2 days. How many travelers will be joining?
```
- **What to say on stage**:
  > *"Budlance understands multilingual and regional Indian phrasing, effortlessly pulling origin, destination, days, and budget from colloquial Tanglish."*

---

## 🎯 Pitch Key Talking Points & Boundaries

1. **Direct Provider Payments**: Budlance plans and hands off; user pays airlines, IRCTC, or hotels directly.
2. **Honesty About Limits**:
   - Trains use standardized Indian Railways distance fare tables because IRCTC does not offer a public booking API.
   - Payments are demonstrated with Stripe sandbox test rails and immediate pass bypass.
   - The product is a working hackathon prototype.
3. **Future Roadmap (From Representation Learning Notes)**:
   - **Semantic Interest Embeddings**: Upgrading tag matching with vector embeddings and cosine similarity in latent space (e.g. matching *"calm misty nature"* directly to hidden mountain attractions).
   - **Razorpay UPI**: Direct UPI intent flow via QR / WhatsApp / Telegram Web App.
   - **Official IRCTC Agent API integration** for live PNR availability.
