# poly-autobetting

Automated **binary pair arbitrage** bot for Polymarket BTC 15-minute prediction markets. It buys both UP and DOWN outcomes when their combined cost is below $1, monitors fills with a real-time WebSocket feed, manages one-sided exposure via an EV-based unwind, and auto-redeems resolved pairs via the gasless relayer.

---

## High-Level Strategy

The bot targets a **15-minute BTC market** with two mutually exclusive outcomes:

- **UP**
- **DOWN**

It tries to buy both sides cheaply enough so that:

- **Total cost of 1 UP + 1 DOWN < $1**
- Since exactly one side settles at $1 and the other at $0, the pair has positive payoff

**Three main layers:**

1. **Entry gate** — Only try entering when the market looks favorable (edge, depth, time).
2. **Fill-state management** — Detect whether neither, one, or both orders fill; track VWAP.
3. **Unwind logic** — If only one side fills, decide whether to wait for the hedge or sell the filled side.

So the full idea: enter only when expected value looks positive; hold completed pairs to resolution; actively manage incomplete one-sided fills to cap damage or lock profit.

---

## Mathematical Summary

### 1. Core pair profit

Let:

- `p_U` = price paid for UP  
- `p_D` = price paid for DOWN  

A complete pair costs:

```
C_pair = p_U + p_D
```

At settlement, exactly one side pays 1 and the other pays 0, so payout = **1**. Pair profit per matched share:

```
π_pair = 1 - (p_U + p_D)
```

**Positive arbitrage exists when:** `p_U + p_D < 1`.

### 2. Position tracking

- UP shares filled = `q_U`, DOWN shares filled = `q_D`
- Hedged shares: `q_H = min(q_U, q_D)`
- UP VWAP = `v_U`, DOWN VWAP = `v_D`
- Combined VWAP per hedged pair: `v_pair = v_U + v_D`

**Hedged profit:**

```
Π_H = q_H × (1 - v_pair)
```

This is the locked-in resolution profit from the fully paired portion.

### 3. Entry condition

- `A_U` = best ask on UP, `A_D` = best ask on DOWN  
- **Combined ask:** `A_combined = A_U + A_D`  
- **Edge:** `edge = 1 - A_combined`

If combined ask is too high or book is too thin, entry is deferred (retryable) or rejected. The bot uses a retryable gate so it can re-check instead of skipping forever.

### 4. One-sided fill problem

Risk: one side fills, the other does not → directional exposure.

If one side filled at average price `a`, and the other (intended hedge) price is `b`, then pair profit per share if the pair eventually completes:

```
π_pair = 1 - a - b - f_pair
```

where `f_pair` is fee/slippage.

### 5. Exit-now value

If the filled side can be sold at bid `s_now`, immediate exit profit per share:

```
π_exit_now = s_now - a - f
```

### 6. Wait vs sell decision

- `p_fill(Δ)` = probability the missing leg fills within next Δ seconds  
- `π_late_exit(Δ)` = expected profit if you must exit later without the hedge  

**EV of waiting Δ more seconds:**

```
EV_wait(Δ) = p_fill(Δ) × π_pair + (1 - p_fill(Δ)) × π_late_exit(Δ)
```

**Decision rule (with buffer β):**

- **Wait** if `EV_wait(Δ) > π_exit_now + β`
- **Sell** if `EV_wait(Δ) ≤ π_exit_now + β`

β is a safety buffer for model error, fees, slippage, and adverse selection.

---

## Algorithm: State Machine & Flow

### State machine

Each market moves through:

| State       | Description                                      |
|------------|---------------------------------------------------|
| **WATCHING**  | Market exists, no position; keep evaluating entry |
| **ENTERED**   | Orders posted; waiting for fills                  |
| **ONE_SIDED** | Exactly one side filled; run unwind logic         |
| **BOTH_FILLED** | Both sides filled; paired profit locked in     |
| **UNWOUND**   | One-sided position sold off                       |
| **DONE**      | Market expired, resolved, or fully redeemed      |

### Full algorithm flow

1. **Discover markets** — Find current/recent BTC 15m markets; subscribe to order books; init as WATCHING.
2. **Entry evaluation** — For each WATCHING market: time remaining, best asks/depth; reject if too late; defer if no edge or thin book; else place both entry orders.
3. **Fill monitoring** — Poll order status; update filled shares, cost basis, VWAP.
4. **State transition** — Neither fill → stay ENTERED; one fill → ONE_SIDED; both fill → BOTH_FILLED.
5. **One-sided unwind** — Compute sell-now value, pair target, fill-probability proxy, EV of waiting; if wait EV is weak, or single-leg profit is good, or time is short: cancel open orders, sell filled side, mark UNWOUND.
6. **Resolution / redemption** — When market closes, redeem winning token balances; mark DONE.

**Intuition:** The bot exploits occasional underpricing of the combined pair and temporary orderbook inefficiencies; it buys convexity cheaply, hedges when possible, and cuts incomplete exposure when the hedge no longer has enough EV.

---

## Edge Cases (Summary)

- **No fill** — No position risk; cancel or let expire.
- **Both fill quickly** — Ideal; profit `Π_H = q_H(1 - v_U - v_D)`; residual risks: fees, settlement.
- **One fill, other never fills** — Main risk; compare EV(wait) vs sell now; sell if waiting no longer dominates.
- **One fill, then price spikes** — If `π_exit_now ≥ π_pair`, sell now (you already captured pair-level profit).
- **Thin/stale book** — Depth and spread filters, EV buffer to avoid fake edge.
- **Fees/slippage** — Real condition is `p_U + p_D + expected costs < 1`.

---

## Features

- **EV-based entry gate** — Retryable; only enters when combined ask and depth look favorable.
- **Configurable limit price & size** — Default 47¢ and 5 shares/side (see `config.json`).
- **VWAP position tracking** — Per-market cost basis and hedged profit.
- **Multi-horizon unwind** — Compares EV of waiting over several time windows vs selling now; uses buffer \( \beta \) and hard timeout.
- **WebSocket order book** — Real-time best bid/ask for unwind and price updates.
- **Trade journal** — Every decision and fill logged with config snapshot to `events.jsonl` and `trade_journal.jsonl` for analysis.
- **Config change detection** — Logs when `config.json` is updated mid-session.
- **Telegram alerts** — Optional: entry, fills, unwinds, heartbeat (set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`); `/kill` to stop.
- **Dry-run mode** — `--dry-run` logs decisions without placing orders; works without credentials using a mock order book.
- **Session analyzer** — `scripts/analyze_dryrun.py` reads the journal and reports fill rates, P&L, config effectiveness, edge/depth breakdown.

---

## Project Structure

```
config.json              # Bot params: price, shares_per_side, max_combined_ask, etc.
scripts/
  place_45.py            # Main bot (entry, fills, unwind, redeem)
  trade_journal.py       # Config-stamped event logging, session stats
  analyze_dryrun.py      # Session report from events/trade_journal
deploy/
  poly-autobetting.service   # systemd unit
  update_and_restart.sh     # Run on server after git pull
  deploy.sh                  # Deploy from local (EC2_HOST=...)
src/
  config.py              # Endpoints, Telegram env
  bot/
    ws_book_feed.py      # WebSocket order book (used by place_45)
    alerts.py            # Telegram (place_45 + legacy)
    # ... (runner, order_engine, etc. for other strategies)
```

---

## Setup

### Prerequisites

- Python 3.10+
- Polymarket account and API credentials

### Installation

```bash
git clone <repo-url>
cd poly-autobetting

python3 -m venv venv
source venv/bin/activate   # or: venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|----------|-------------|
| `POLYMARKET_PRIVATE_KEY` | Polygon wallet private key |
| `POLYMARKET_FUNDER` | Funder/proxy address (if needed) |
| `POLYMARKET_BUILDER_*` | Builder relayer (optional; for gasless redeems) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional Telegram alerts |

Edit `config.json` for strategy params (price, shares per side, max combined ask, unwind buffer, timeouts, etc.).

---

## Usage

```bash
# Live
python scripts/place_45.py

# Dry-run (no real orders; mock book if no credentials)
python scripts/place_45.py --dry-run

# After a session, analyze
python scripts/analyze_dryrun.py
```

The bot loops every ~10s: discover markets, evaluate entry, monitor fills, run unwind logic, redeem closed markets.

---

## Short Summary

The bot buys both sides of a binary UP/DOWN market only when total expected cost is low enough. If both sides fill, profit per pair is `1 - (p_U + p_D)`. If only one fills, it compares immediate sell value to the expected value of waiting for the hedge, and exits when `EV_wait ≤ π_exit_now + β`. So: enter when edge exists, hold completed pairs, unwind incomplete fills when waiting is no longer worth it.

---

## Disclaimer

This software is for educational purposes. Trading on prediction markets involves risk. Use at your own discretion.
