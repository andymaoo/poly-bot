"""
Analyze V2 dry-run events.jsonl and produce a structured report.

Reads events from the V2 bot (market_discovered, market_deferred, market_entered,
fill_detected, one_sided_fill, both_filled, unwind_check, unwind_sold, etc.),
builds per-market timelines and aggregate stats, and writes:
  - data/dryrun_report.json  (full machine-readable report)
  - stdout summary

Usage:
  python scripts/analyze_dryrun.py [path/to/events.jsonl]
  Default: PROJECT_ROOT/events.jsonl
"""

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

DEFAULT_EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.jsonl")
REPORT_DIR = os.path.join(PROJECT_ROOT, "data")
REPORT_PATH = os.path.join(REPORT_DIR, "dryrun_report.json")


def load_events(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def get_market_ts(event: dict) -> int | None:
    """Extract market_ts from event (payload or top-level)."""
    p = event.get("payload") or {}
    return p.get("market_ts") or event.get("market_ts")


def get_slug(event: dict) -> str | None:
    p = event.get("payload") or {}
    return p.get("slug") or event.get("slug")


@dataclass
class MarketRecord:
    market_ts: int
    slug: str = ""
    title: str = ""
    state_timeline: list[dict] = field(default_factory=list)
    deferred_count: int = 0
    deferred_reasons: list[str] = field(default_factory=list)
    first_discovered_ts: int = 0
    entered_ts: int = 0
    time_in_watching_sec: float = 0.0
    edge_at_entry: float = 0.0
    combined_ask_at_entry: float = 0.0
    orders_placed: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    one_sided_ts: int = 0
    filled_side: str = ""
    filled_shares: float = 0.0
    both_filled_ts: int = 0
    unwind_checks: list[dict] = field(default_factory=list)
    unwind_sold_ts: int = 0
    unwind_trigger: str = ""
    simulated_exit_profit: float = 0.0
    price_snapshots: list[dict] = field(default_factory=list)
    outcome: str = ""  # entered|expired|no_fills|both_filled|unwound
    redeemed: bool = False


def analyze(events: list[dict], path: str) -> dict[str, Any]:
    if not events:
        return {
            "error": "No events",
            "events_path": path,
            "total_events": 0,
            "markets": {},
            "aggregate": {},
        }

    first_ts = min(e.get("ts", 0) for e in events)
    last_ts = max(e.get("ts", 0) for e in events)
    markets: dict[int, MarketRecord] = {}
    config_snapshot: dict = {}

    for e in events:
        ts = e.get("ts", 0)
        typ = e.get("type", "")
        payload = e.get("payload") or {}

        if typ == "status":
            config_snapshot = payload.get("config") or {}
            continue

        market_ts = get_market_ts(e)
        slug = get_slug(e) or (f"market-{market_ts}" if market_ts else "")

        if market_ts is None and typ not in ("price_update",):
            continue

        if market_ts is not None and market_ts not in markets:
            markets[market_ts] = MarketRecord(market_ts=market_ts, slug=slug)

        m = markets.get(market_ts) if market_ts else None
        if m is None:
            continue

        if typ == "market_discovered":
            m.slug = slug
            m.title = payload.get("title", "")
            m.first_discovered_ts = ts
            m.state_timeline.append({"ts": ts, "state": "discovered"})

        elif typ == "market_deferred":
            m.deferred_count += 1
            m.deferred_reasons.append(payload.get("reason", "unknown"))
            m.state_timeline.append({"ts": ts, "state": "deferred", "reason": payload.get("reason")})

        elif typ == "market_expired":
            m.outcome = "expired"
            m.state_timeline.append({"ts": ts, "state": "expired", "reason": payload.get("reason")})

        elif typ == "edge_check":
            m.edge_at_entry = payload.get("edge", 0)
            m.combined_ask_at_entry = payload.get("combined_ask", 0)

        elif typ == "order_placed":
            m.orders_placed.append({
                "ts": ts, "side": payload.get("side"), "price": payload.get("price"),
                "shares": payload.get("shares"), "order_id": payload.get("order_id"),
            })

        elif typ == "market_entered":
            m.entered_ts = ts
            if m.first_discovered_ts:
                m.time_in_watching_sec = ts - m.first_discovered_ts
            m.state_timeline.append({"ts": ts, "state": "entered"})

        elif typ == "fill_detected":
            m.fills.append({
                "ts": ts, "side": payload.get("side"), "delta": payload.get("delta"),
                "total_matched": payload.get("total_matched"), "price": payload.get("price"),
            })

        elif typ == "one_sided_fill":
            m.one_sided_ts = ts
            m.filled_side = payload.get("filled_side", "")
            m.filled_shares = payload.get("filled_shares", 0)
            m.state_timeline.append({"ts": ts, "state": "one_sided", "filled_side": m.filled_side})

        elif typ == "both_filled":
            m.both_filled_ts = ts
            m.outcome = "both_filled"
            m.state_timeline.append({
                "ts": ts, "state": "both_filled",
                "hedged_profit": payload.get("hedged_profit"), "combined_vwap": payload.get("combined_vwap"),
            })

        elif typ == "unwind_check":
            m.unwind_checks.append({
                "ts": ts,
                "pi_exit_now": payload.get("pi_exit_now"),
                "pi_pair": payload.get("pi_pair"),
                "p_fill": payload.get("p_fill"),
                "ev_wait": payload.get("ev_wait"),
                "time_remaining": payload.get("time_remaining"),
                "trigger": payload.get("trigger"),
                "filled_side": payload.get("filled_side"),
                "s_now": payload.get("s_now"),
            })

        elif typ == "unwind_sold":
            m.unwind_sold_ts = ts
            m.outcome = "unwound"
            last_check = m.unwind_checks[-1] if m.unwind_checks else {}
            m.unwind_trigger = last_check.get("trigger", "unknown")
            shares = payload.get("shares", 0)
            pi_exit = last_check.get("pi_exit_now", 0)
            m.simulated_exit_profit = shares * pi_exit if (shares and pi_exit) else 0
            m.state_timeline.append({"ts": ts, "state": "unwound", "trigger": m.unwind_trigger})

        elif typ == "market_no_fills":
            m.outcome = "no_fills"
            m.state_timeline.append({"ts": ts, "state": "no_fills"})

        elif typ == "price_update" and market_ts:
            m.price_snapshots.append({
                "ts": ts,
                "up_ask": payload.get("up_ask"),
                "dn_ask": payload.get("dn_ask"),
            })

        elif typ == "redeem_started" and market_ts:
            m.redeemed = True

    # Infer outcome for markets that entered but have no explicit outcome
    for m in markets.values():
        if m.entered_ts and not m.outcome:
            if m.both_filled_ts:
                m.outcome = "both_filled"
            elif m.unwind_sold_ts:
                m.outcome = "unwound"
            elif m.one_sided_ts and not m.unwind_sold_ts:
                m.outcome = "one_sided_no_unwind"
            else:
                m.outcome = "entered_unknown"

    # Aggregate stats
    discovered = set(markets.keys())
    entered = {mt for mt, m in markets.items() if m.entered_ts > 0}
    deferred_any = {mt for mt, m in markets.items() if m.deferred_count > 0}
    deferred_then_entered = {mt for mt in entered if mt in deferred_any}
    expired = {mt for mt, m in markets.items() if m.outcome == "expired"}
    no_fills = {mt for mt, m in markets.items() if m.outcome == "no_fills"}
    both_filled = {mt for mt, m in markets.items() if m.outcome == "both_filled"}
    unwound = {mt for mt, m in markets.items() if m.outcome == "unwound"}
    with_fill = {mt for mt, m in markets.items() if m.fills or m.one_sided_ts or m.both_filled_ts}
    unwind_triggers = defaultdict(int)
    for m in markets.values():
        if m.unwind_trigger:
            unwind_triggers[m.unwind_trigger] += 1

    time_in_watching = [m.time_in_watching_sec for m in markets.values() if m.time_in_watching_sec > 0]
    time_in_one_sided = []
    for m in markets.values():
        if m.one_sided_ts and m.unwind_sold_ts:
            time_in_one_sided.append(m.unwind_sold_ts - m.one_sided_ts)
    simulated_pnl = sum(m.simulated_exit_profit for m in markets.values())

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0

    aggregate = {
        "total_events": len(events),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_sec": last_ts - first_ts,
        "duration_hours": round((last_ts - first_ts) / 3600, 2),
        "markets_discovered": len(discovered),
        "markets_entered": len(entered),
        "markets_expired": len(expired),
        "markets_no_fills": len(no_fills),
        "markets_both_filled": len(both_filled),
        "markets_unwound": len(unwound),
        "markets_with_any_fill": len(with_fill),
        "entry_pass_rate": round(len(entered) / len(discovered), 4) if discovered else 0,
        "deferred_then_entered_count": len(deferred_then_entered),
        "deferred_then_entered_rate": round(len(deferred_then_entered) / len(deferred_any), 4) if deferred_any else 0,
        "fill_rate": round(len(with_fill) / len(entered), 4) if entered else 0,
        "both_filled_rate": round(len(both_filled) / len(entered), 4) if entered else 0,
        "one_sided_rate": round((len(with_fill) - len(both_filled)) / len(entered), 4) if entered else 0,
        "unwind_trigger_distribution": dict(unwind_triggers),
        "avg_time_in_watching_sec": round(avg(time_in_watching), 1),
        "avg_time_in_one_sided_before_unwind_sec": round(avg(time_in_one_sided), 1),
        "simulated_unwind_pnl_usd": round(simulated_pnl, 2),
        "config_snapshot": config_snapshot,
    }

    # Serializable per-market data
    markets_serializable = {}
    for mt, m in markets.items():
        markets_serializable[mt] = {
            "market_ts": m.market_ts,
            "slug": m.slug,
            "title": m.title,
            "state_timeline": m.state_timeline,
            "deferred_count": m.deferred_count,
            "deferred_reasons": m.deferred_reasons,
            "first_discovered_ts": m.first_discovered_ts,
            "entered_ts": m.entered_ts,
            "time_in_watching_sec": round(m.time_in_watching_sec, 1),
            "edge_at_entry": m.edge_at_entry,
            "combined_ask_at_entry": m.combined_ask_at_entry,
            "orders_placed": m.orders_placed,
            "fills": m.fills,
            "one_sided_ts": m.one_sided_ts,
            "filled_side": m.filled_side,
            "filled_shares": m.filled_shares,
            "both_filled_ts": m.both_filled_ts,
            "unwind_checks": m.unwind_checks,
            "unwind_sold_ts": m.unwind_sold_ts,
            "unwind_trigger": m.unwind_trigger,
            "simulated_exit_profit": round(m.simulated_exit_profit, 2),
            "outcome": m.outcome,
            "redeemed": m.redeemed,
            "price_snapshots_count": len(m.price_snapshots),
        }

    return {
        "events_path": path,
        "total_events": len(events),
        "aggregate": aggregate,
        "markets": markets_serializable,
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EVENTS_PATH
    events = load_events(path)
    result = analyze(events, path)

    if result.get("error"):
        print(result["error"])
        print("Path used:", path)
        sys.exit(1)

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print("Report written to:", REPORT_PATH)
    print()

    agg = result["aggregate"]
    print("=" * 60)
    print("DRY-RUN AGGREGATE SUMMARY")
    print("=" * 60)
    print("Events path:     ", result["events_path"])
    print("Total events:    ", result["total_events"])
    print("Duration:        ", agg["duration_hours"], "hours")
    print()
    print("Markets discovered:  ", agg["markets_discovered"])
    print("Markets entered:     ", agg["markets_entered"])
    print("Markets expired:     ", agg["markets_expired"])
    print("Markets no_fills:    ", agg["markets_no_fills"])
    print("Markets both_filled: ", agg["markets_both_filled"])
    print("Markets unwound:     ", agg["markets_unwound"])
    print("Markets with any fill:", agg["markets_with_any_fill"])
    print()
    print("Entry pass rate:         ", agg["entry_pass_rate"])
    print("Deferred then entered:   ", agg["deferred_then_entered_count"], "(", agg["deferred_then_entered_rate"], ")")
    print("Fill rate:               ", agg["fill_rate"])
    print("Both-filled rate:        ", agg["both_filled_rate"])
    print("One-sided rate:          ", agg["one_sided_rate"])
    print()
    print("Unwind trigger distribution:", agg["unwind_trigger_distribution"])
    print("Avg time in WATCHING (sec):  ", agg["avg_time_in_watching_sec"])
    print("Avg time one_sided->unwind: ", agg["avg_time_in_one_sided_before_unwind_sec"], "sec")
    print("Simulated unwind PnL (USD): ", agg["simulated_unwind_pnl_usd"])
    print("=" * 60)


if __name__ == "__main__":
    main()
