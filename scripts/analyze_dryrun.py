"""
Analyze V2 bot sessions from trade_journal.jsonl or events.jsonl.

Reads config-stamped events, builds per-market timelines, and produces:
  - Aggregate stats (fill rate, P&L, config effectiveness)
  - Per-config-version breakdown
  - Book condition correlation with outcomes
  - Time-of-day analysis
  - data/dryrun_report.json (full machine-readable report)
  - stdout summary

Usage:
  python scripts/analyze_dryrun.py                          # auto-detect journal
  python scripts/analyze_dryrun.py [path/to/events.jsonl]   # explicit path
"""

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

JOURNAL_PATH = os.path.join(PROJECT_ROOT, "trade_journal.jsonl")
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
    p = event.get("payload") or {}
    return p.get("market_ts") or event.get("market_ts")


def get_slug(event: dict) -> str | None:
    p = event.get("payload") or {}
    return p.get("slug") or event.get("slug")


def get_config_version(event: dict) -> int:
    return event.get("config_version", 0)


def get_config(event: dict) -> dict:
    return event.get("config") or {}


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
    ev_at_entry: float = 0.0
    book_snapshot_at_entry: dict = field(default_factory=dict)
    config_at_entry: dict = field(default_factory=dict)
    config_version_at_entry: int = 0
    orders_placed: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    one_sided_ts: int = 0
    filled_side: str = ""
    filled_shares: float = 0.0
    both_filled_ts: int = 0
    unwind_checks: list[dict] = field(default_factory=list)
    unwind_sold_ts: int = 0
    unwind_trigger: str = ""
    unwind_exit_price: float = 0.0
    realized_pnl: float = 0.0
    simulated_exit_profit: float = 0.0
    price_snapshots: list[dict] = field(default_factory=list)
    outcome: str = ""
    redeemed: bool = False
    capital_deployed: float = 0.0


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
    config_versions: dict[int, dict] = {}

    for e in events:
        ts = e.get("ts", 0)
        typ = e.get("type", "")
        payload = e.get("payload") or {}
        cv = get_config_version(e)
        cfg = get_config(e)

        if cv and cfg and cv not in config_versions:
            config_versions[cv] = cfg

        if typ == "status":
            p_cfg = payload.get("config") or cfg
            if p_cfg:
                config_snapshot = p_cfg
            continue

        if typ == "config_changed":
            continue

        if typ == "session_start":
            continue

        if typ == "heartbeat":
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
            m.ev_at_entry = payload.get("ev_entry", 0)
            m.book_snapshot_at_entry = payload.get("book_snapshot", {})
            m.config_at_entry = cfg
            m.config_version_at_entry = cv

        elif typ == "order_placed":
            cost = payload.get("cost", 0)
            if not cost:
                cost = (payload.get("shares", 0) or 0) * (payload.get("price", 0) or 0)
            m.capital_deployed += cost
            m.orders_placed.append({
                "ts": ts, "side": payload.get("side"), "price": payload.get("price"),
                "shares": payload.get("shares"), "order_id": payload.get("order_id"),
                "cost": round(cost, 4),
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
            hp = payload.get("hedged_profit", 0) or 0
            m.realized_pnl = hp
            m.state_timeline.append({
                "ts": ts, "state": "both_filled",
                "hedged_profit": hp, "combined_vwap": payload.get("combined_vwap"),
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
            m.unwind_trigger = last_check.get("trigger", payload.get("trigger", "unknown"))
            m.unwind_exit_price = payload.get("exit_price", 0) or 0
            rpnl = payload.get("realized_pnl", 0)
            if rpnl:
                m.realized_pnl = rpnl
            else:
                shares = payload.get("shares", 0)
                pi_exit = last_check.get("pi_exit_now", 0)
                m.simulated_exit_profit = shares * pi_exit if (shares and pi_exit) else 0
                m.realized_pnl = m.simulated_exit_profit
            m.state_timeline.append({"ts": ts, "state": "unwound", "trigger": m.unwind_trigger})

        elif typ == "market_no_fills":
            m.outcome = "no_fills"
            m.state_timeline.append({"ts": ts, "state": "no_fills"})

        elif typ == "entry_cancelled_late":
            m.outcome = "no_fills"
            m.state_timeline.append({"ts": ts, "state": "cancelled_late"})

        elif typ == "price_update" and market_ts:
            m.price_snapshots.append({
                "ts": ts,
                "up_ask": payload.get("up_ask"),
                "dn_ask": payload.get("dn_ask"),
            })

        elif typ == "redeem_started" and market_ts:
            m.redeemed = True

        elif typ == "redeem_confirmed" and market_ts:
            m.redeemed = True

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

    # ── Aggregate stats ──────────────────────────────────────────────

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

    total_pnl = sum(m.realized_pnl for m in markets.values())
    total_capital = sum(m.capital_deployed for m in markets.values())

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
        "total_realized_pnl_usd": round(total_pnl, 4),
        "total_capital_deployed_usd": round(total_capital, 2),
        "roi_pct": round((total_pnl / total_capital * 100), 2) if total_capital > 0 else 0,
        "config_snapshot": config_snapshot,
    }

    # ── Per-config-version breakdown ─────────────────────────────────

    cv_groups: dict[int, list[MarketRecord]] = defaultdict(list)
    for m in markets.values():
        cv_groups[m.config_version_at_entry or 0].append(m)

    config_breakdown = {}
    for cv, group in sorted(cv_groups.items()):
        g_entered = [m for m in group if m.entered_ts]
        g_filled = [m for m in group if m.fills or m.one_sided_ts or m.both_filled_ts]
        g_both = [m for m in group if m.outcome == "both_filled"]
        g_unwound = [m for m in group if m.outcome == "unwound"]
        g_nofill = [m for m in group if m.outcome == "no_fills"]
        g_pnl = sum(m.realized_pnl for m in group)
        g_cap = sum(m.capital_deployed for m in group)
        config_breakdown[cv] = {
            "config": config_versions.get(cv, {}),
            "markets": len(group),
            "entered": len(g_entered),
            "filled": len(g_filled),
            "both_filled": len(g_both),
            "unwound": len(g_unwound),
            "no_fills": len(g_nofill),
            "fill_rate": round(len(g_filled) / len(g_entered), 4) if g_entered else 0,
            "pnl": round(g_pnl, 4),
            "capital": round(g_cap, 2),
            "roi_pct": round(g_pnl / g_cap * 100, 2) if g_cap > 0 else 0,
        }

    # ── Edge tier analysis ───────────────────────────────────────────

    edge_buckets = {"negative": [], "0-2c": [], "2-5c": [], "5c+": []}
    for m in markets.values():
        e = m.edge_at_entry
        if e < 0:
            edge_buckets["negative"].append(m)
        elif e < 0.02:
            edge_buckets["0-2c"].append(m)
        elif e < 0.05:
            edge_buckets["2-5c"].append(m)
        else:
            edge_buckets["5c+"].append(m)

    edge_analysis = {}
    for bucket, group in edge_buckets.items():
        g_entered = [m for m in group if m.entered_ts]
        g_filled = [m for m in group if m.fills or m.one_sided_ts or m.both_filled_ts]
        edge_analysis[bucket] = {
            "count": len(group),
            "entered": len(g_entered),
            "filled": len(g_filled),
            "fill_rate": round(len(g_filled) / len(g_entered), 4) if g_entered else 0,
            "avg_edge": round(avg([m.edge_at_entry for m in group]), 4) if group else 0,
            "pnl": round(sum(m.realized_pnl for m in group), 4),
        }

    # ── Book depth analysis ──────────────────────────────────────────

    depth_analysis = {"thin": [], "moderate": [], "deep": []}
    for m in markets.values():
        snap = m.book_snapshot_at_entry
        if not snap:
            continue
        up_depth = sum(lvl.get("size", 0) for lvl in snap.get("up_asks", []))
        dn_depth = sum(lvl.get("size", 0) for lvl in snap.get("dn_asks", []))
        total = up_depth + dn_depth
        if total < 20:
            depth_analysis["thin"].append(m)
        elif total < 60:
            depth_analysis["moderate"].append(m)
        else:
            depth_analysis["deep"].append(m)

    depth_summary = {}
    for bucket, group in depth_analysis.items():
        g_filled = [m for m in group if m.fills or m.one_sided_ts or m.both_filled_ts]
        depth_summary[bucket] = {
            "count": len(group),
            "filled": len(g_filled),
            "fill_rate": round(len(g_filled) / len(group), 4) if group else 0,
            "pnl": round(sum(m.realized_pnl for m in group), 4),
        }

    # ── Time-of-day analysis ─────────────────────────────────────────

    hour_buckets: dict[int, list[MarketRecord]] = defaultdict(list)
    for m in markets.values():
        if m.entered_ts:
            dt = datetime.fromtimestamp(m.entered_ts, tz=timezone.utc)
            hour_buckets[dt.hour].append(m)

    time_analysis = {}
    for hour in sorted(hour_buckets.keys()):
        group = hour_buckets[hour]
        g_filled = [m for m in group if m.fills or m.one_sided_ts or m.both_filled_ts]
        time_analysis[f"{hour:02d}:00"] = {
            "entered": len(group),
            "filled": len(g_filled),
            "fill_rate": round(len(g_filled) / len(group), 4) if group else 0,
            "pnl": round(sum(m.realized_pnl for m in group), 4),
        }

    # ── Serializable per-market data ─────────────────────────────────

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
            "ev_at_entry": m.ev_at_entry,
            "config_version_at_entry": m.config_version_at_entry,
            "config_at_entry": m.config_at_entry,
            "book_snapshot_at_entry": m.book_snapshot_at_entry,
            "orders_placed": m.orders_placed,
            "capital_deployed": round(m.capital_deployed, 4),
            "fills": m.fills,
            "one_sided_ts": m.one_sided_ts,
            "filled_side": m.filled_side,
            "filled_shares": m.filled_shares,
            "both_filled_ts": m.both_filled_ts,
            "unwind_checks": m.unwind_checks,
            "unwind_sold_ts": m.unwind_sold_ts,
            "unwind_trigger": m.unwind_trigger,
            "unwind_exit_price": m.unwind_exit_price,
            "realized_pnl": round(m.realized_pnl, 4),
            "outcome": m.outcome,
            "redeemed": m.redeemed,
            "price_snapshots_count": len(m.price_snapshots),
        }

    return {
        "events_path": path,
        "total_events": len(events),
        "aggregate": aggregate,
        "config_breakdown": config_breakdown,
        "edge_analysis": edge_analysis,
        "depth_analysis": depth_summary,
        "time_analysis": time_analysis,
        "config_versions": {str(k): v for k, v in config_versions.items()},
        "markets": markets_serializable,
    }


def print_summary(result: dict) -> None:
    agg = result["aggregate"]
    print("=" * 60)
    print("SESSION ANALYSIS REPORT")
    print("=" * 60)
    print(f"Events path:      {result['events_path']}")
    print(f"Total events:     {result['total_events']}")
    print(f"Duration:         {agg['duration_hours']} hours")
    print()

    print("-- Markets ----------------------------------------------")
    print(f"  Discovered:     {agg['markets_discovered']}")
    print(f"  Entered:        {agg['markets_entered']}")
    print(f"  Expired:        {agg['markets_expired']}")
    print(f"  No fills:       {agg['markets_no_fills']}")
    print(f"  Both filled:    {agg['markets_both_filled']}")
    print(f"  Unwound:        {agg['markets_unwound']}")
    print(f"  Any fill:       {agg['markets_with_any_fill']}")
    print()

    print("-- Rates ------------------------------------------------")
    print(f"  Entry pass:     {agg['entry_pass_rate']:.1%}")
    print(f"  Fill rate:      {agg['fill_rate']:.1%}")
    print(f"  Both-filled:    {agg['both_filled_rate']:.1%}")
    print(f"  One-sided:      {agg['one_sided_rate']:.1%}")
    print()

    print("-- P&L --------------------------------------------------")
    print(f"  Realized PnL:   ${agg['total_realized_pnl_usd']:+.4f}")
    print(f"  Capital used:   ${agg['total_capital_deployed_usd']:.2f}")
    print(f"  ROI:            {agg['roi_pct']:+.2f}%")
    print()

    print("-- Timing -----------------------------------------------")
    print(f"  Avg WATCHING:   {agg['avg_time_in_watching_sec']:.1f}s")
    print(f"  Avg ONE_SIDED:  {agg['avg_time_in_one_sided_before_unwind_sec']:.1f}s")
    print(f"  Unwind triggers: {agg['unwind_trigger_distribution']}")
    print()

    cb = result.get("config_breakdown", {})
    if cb:
        print("-- Config Versions -------------------------------------")
        for cv, data in cb.items():
            cfg = data.get("config", {})
            price = cfg.get("price", "?")
            shares = cfg.get("shares_per_side", "?")
            print(f"  v{cv} (price={price}, shares={shares}):")
            print(f"    entered={data['entered']} filled={data['filled']} "
                  f"both={data['both_filled']} nofill={data['no_fills']}")
            print(f"    fill_rate={data['fill_rate']:.1%}  "
                  f"pnl=${data['pnl']:+.4f}  roi={data['roi_pct']:+.2f}%")
        print()

    ea = result.get("edge_analysis", {})
    if ea:
        print("-- Edge Tiers -----------------------------------------")
        for bucket, data in ea.items():
            if data["count"] == 0:
                continue
            print(f"  {bucket:>8s}: {data['count']} mkts, "
                  f"fill={data['fill_rate']:.0%}, "
                  f"avg_edge={data['avg_edge']:.4f}, "
                  f"pnl=${data['pnl']:+.4f}")
        print()

    da = result.get("depth_analysis", {})
    if da:
        print("-- Book Depth ------------------------------------------")
        for bucket, data in da.items():
            if data["count"] == 0:
                continue
            print(f"  {bucket:>8s}: {data['count']} mkts, "
                  f"fill={data['fill_rate']:.0%}, "
                  f"pnl=${data['pnl']:+.4f}")
        print()

    ta = result.get("time_analysis", {})
    if ta:
        print("-- Time of Day (UTC) -----------------------------------")
        for hour, data in ta.items():
            print(f"  {hour}: entered={data['entered']} "
                  f"filled={data['filled']} "
                  f"fill={data['fill_rate']:.0%} "
                  f"pnl=${data['pnl']:+.4f}")
        print()

    print("=" * 60)


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    elif os.path.isfile(JOURNAL_PATH):
        path = JOURNAL_PATH
    else:
        path = DEFAULT_EVENTS_PATH

    events = load_events(path)
    result = analyze(events, path)

    if result.get("error"):
        print(result["error"])
        print("Path used:", path)
        sys.exit(1)

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"Report written to: {REPORT_PATH}")
    print()
    print_summary(result)


if __name__ == "__main__":
    main()
