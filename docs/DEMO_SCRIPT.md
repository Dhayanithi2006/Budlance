# 🎬 Budlance Live Hackathon Demo Script

> **Golden Rule for Live Demo**: Do **not** improvise messages. Copy-paste or type these exact verified prompts in order. Everything runs cleanly against verified live/cached data, strict budget bounds, and virtual ledger accounting.

---

## ⏱️ Live Presentation Timeline (Total: 4–5 Minutes)

| Time | Segment | Action / Screen | Key Message |
|------|---------|-----------------|-------------|
| **0:00 - 0:45** | **The Problem & Value Prop** | Title slide / Opening | Most travel apps start with a destination and inflate the bill. Budlance is a **reverse-budget AI travel agent** that starts from what you can afford, enforces feasibility upfront, and hands off directly to providers. |
| **0:45 - 2:00** | **Scenario 1: Munnar Plan & Trip Pass** | Telegram Bot | Couple trip to Munnar (₹1,00,000, 5 days). Preview costs, locked links, unlock via Trip Pass (Stripe test card or `/demo_pass`), real flight & hotel with ratings/reviews. |
| **2:00 - 3:00** | **Scenario 2: In-Trip Adaptation & Crowd Swap** | Telegram Bot | Movie night addition, crowd swap rescue from crowded lake to Wonder Valley, taxi fare calculated and deducted from emergency buffer. |
| **3:00 - 3:45** | **Scenario 3: Expense Logging & Reconciliation** | Telegram Bot | Log Day 1 expenses against buffer, complete trip, final reconciliation with zero hallucination. |
| **3:45 - 4:30** | **Pitch Limits & Roadmap** | Roadmap Slide | State boundaries with integrity (no fake live data, prototype status). Pitch vector embeddings for interest matching, UPI intent integration, and IRCTC agent API. |

---

## 📝 The Exact 11-Step Master Demo Sequence

### Step 1: `/start`
- **User sends:**
  ```text
  /start
  ```
- **Expected Bot Reply:**
  ```text
  Welcome to Budlance — your Reverse-Budget AI Travel Agent! 🌍
  Tell me where you want to travel, your budget, number of travellers, and what you enjoy.
  ```

---

### Step 2: Munnar Trip Discovery & Dates
- **User sends:**
  ```text
  Plan a trip to Munnar from Chennai for 2 people (couple), budget 100000, 5 days, we like calm nature and sunrise
  ```
- **Bot Response:**
  ```text
  Which date do you want to start? (Example: 9 Nov)
  ```
- **User sends:**
  ```text
  9 Nov
  ```
- **Bot Response (Plan Preview):**
  - Dates: Mon 9 Nov – Fri 13 Nov 2026 | 5 days, 4 nights | 2 travellers | from Chennai
  - Flights Chennai to Kochi (estimate): Rs 6,000
  - Taxi Kochi to Munnar and back (estimate): Rs 5,720
  - Stay, 4 nights (estimate): Rs 20,000
  - Food and local travel (estimate): Rs 9,000
  - Activities (estimate): Rs 5,000
  - Emergency buffer: Rs 5,000
  - Total planned: Rs 50,720 of your Rs 100,000. Rs 49,280 is left.
  - Highlights: The Blossom Hydel Park (⭐ 4.0), Tea Museum (⭐ 3.8).
  - Transit statement: Munnar has no airport, so you fly to Kochi and drive about 3.5 hours each way.
  - Call to action: Unlock full plan with flight and hotel links for Rs 49.

---

### Step 3: Locked Links Guard
- **User sends:**
  ```text
  send me the flight link
  ```
- **Bot Response:**
  - Shows locked message with payment link (Stripe / Trip Pass).
  - Refuses to leak bare flight or hotel booking links before unlock.

---

### Step 4: Unverified Payment Refusal
- **User sends (before paying):**
  ```text
  paid
  ```
- **Bot Response:**
  - Verifies payment against backend; detects payment is pending.
  - Refuses activation with friendly message: *"I haven't received confirmation for your payment yet. Complete checkout using the link above, or type /demo_pass for demo evaluation."*

---

### Step 5: Complete Payment
- **Action:** User pays via Stripe Checkout using test card `4242 4242 4242 4242` (or sends `/demo_pass` in demo mode), then sends:
  ```text
  paid
  ```
- **Bot Response:**
  - Confirms payment receipt and unlocks the Trip Pass.

---

### Step 6: Unlocked Full Plan
- **Bot Response:**
  - Real carrier: IndiGo 6E 6216.
  - Real hotel: Tea County Munnar (⭐ 4.5, 4,844 reviews).
  - Verified transit and booking links.
  - "Prices checked" timestamp.

---

### Step 7: Dynamic Modification (Movie Night)
- **User sends:**
  ```text
  add a movie night on day 2
  ```
- **Bot Response:**
  - Day 2 evening itinerary updated with movie / cinema activity in Munnar.
  - Preserves fixed flight and hotel bookings.

---

### Step 8: In-Trip Crowd Swap Rescue
- **User sends:**
  ```text
  too many people here, find a theme park
  ```
- **Bot Response (Proposal):**
  ```text
  No theme park found in Munnar. The closest alternative is Wonder Valley Adventure and Amusement Park (~14 km away).
  Taxi to Wonder Valley (estimate): Rs 308
  New emergency buffer balance: Rs 9,692
  Go ahead? yes or no
  ```
- **User sends:**
  ```text
  yes
  ```
- **Bot Response (Confirmed):**
  ```text
  Plan updated. Swapped with Wonder Valley Adventure and Amusement Park. Taxi (estimate): Rs 308. New emergency buffer balance: Rs 9,692.
  ```

---

### Step 9: In-Trip Query
- **User sends:**
  ```text
  what is today's plan?
  ```
- **Bot Response:**
  - Summarizes current day schedule, activities, and remaining daily allowance.

---

### Step 10: Activation & Expense Logging
- **User sends:**
  ```text
  booked
  ```
- **User sends:**
  ```text
  spent 1200 on lunch and tickets
  ```
- **Bot Response:**
  - Records Rs 1,200 in the Virtual Ledger.
  - Updates remaining day buffer without altering fixed commitments.

---

### Step 11: Trip Completion & Reconciliation
- **User sends:**
  ```text
  trip is done
  ```
- **Bot Response:**
  ```text
  Your planned trip budget was Rs 100,000.
  Would you like to record your final actual spend? You can also say skip.
  ```
- **User sends:**
  ```text
  20000
  ```
- **Bot Response:**
  - Final reconciliation summary:
    - Planned budget: Rs 100,000
    - Recorded actual spend: Rs 20,000
    - Difference: Rs 80,000 under planned budget
    - Notes: Recorded actual spending is user-reported.

---

## 🎯 Pitch Key Talking Points & Boundaries

1. **Direct Provider Payments**: Budlance plans and hands off; user pays airlines, IRCTC, or hotels directly.
2. **Honesty About Limits**:
   - Trains use standardized Indian Railways distance fare tables because IRCTC does not offer a public booking API.
   - Payments are demonstrated with Stripe sandbox test rails and immediate pass bypass.
   - The product is a working hackathon prototype.
3. **Future Roadmap**:
   - **Semantic Interest Embeddings**: Upgrading tag matching with vector embeddings and cosine similarity in latent space.
   - **Razorpay UPI**: Direct UPI intent flow via QR / WhatsApp / Telegram Web App.
   - **Official IRCTC Agent API integration** for live PNR availability.
