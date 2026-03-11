"""
Trade journal for place_45 bot -- config-stamped event logging, session tracking,
order book snapshots, P&L ledger, and config change detection.

Wraps the raw emit_event() with richer payloads so every decision can be
traced back to the exact config and market conditions that produced it.
"""

import json
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_PATH = os.path.join(PROJECT_ROOT, "trade_journal.jsonl")
EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.jsonl")


def _write_line(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def snap_book_levels(book, side: str = "asks", depth: int = 3) -> List[dict]:
    """Extract top N price levels from an order book side."""
    levels = getattr(book, side, []) if book else []
    out = []
    for lvl in levels[:depth]:
        out.append({
            "price": round(float(lvl.price), 4),
            "size": round(float(lvl.size), 1),
        })
    return out


class TradeJournal:
    """Enriched event logger that stamps every event with config and session context."""

    def __init__(self, cfg_dict: dict, dry_run: bool = False):
        self._cfg = dict(cfg_dict)
        self._dry_run = dry_run
        self._session_start = int(time.time())
        self._config_version = 1
        self._prev_cfg = dict(cfg_dict)

        self._markets_discovered = 0
        self._markets_entered = 0
        self._markets_filled = 0
        self._markets_both_filled = 0
        self._markets_unwound = 0
        self._markets_no_fills = 0
        self._total_realized_pnl = 0.0
        self._total_capital_deployed = 0.0
        self._heartbeat_counter = 0

        self._market_pnl: Dict[int, dict] = {}

        self._emit("session_start", {
            "dry_run": dry_run,
            "session_start_ts": self._session_start,
        })

    @property
    def config_dict(self) -> dict:
        return dict(self._cfg)

    def update_config(self, new_cfg_dict: dict) -> Optional[dict]:
        """Detect config changes. Returns diff dict if changed, else None."""
        changes = {}
        for k, v in new_cfg_dict.items():
            old = self._prev_cfg.get(k)
            if old != v:
                changes[k] = {"old": old, "new": v}
        if changes:
            self._config_version += 1
            self._prev_cfg = dict(new_cfg_dict)
            self._cfg = dict(new_cfg_dict)
            self._emit("config_changed", {
                "config_version": self._config_version,
                "changes": changes,
            })
            return changes
        self._cfg = dict(new_cfg_dict)
        return None

    def _emit(self, event_type: str, payload: dict) -> None:
        """Write to both events.jsonl (backwards-compat) and trade_journal.jsonl."""
        ts = int(time.time())
        base = {"type": event_type, "ts": ts, "payload": payload}
        _write_line(EVENTS_PATH, base)

        enriched = {
            "type": event_type,
            "ts": ts,
            "session_start": self._session_start,
            "config_version": self._config_version,
            "config": self._cfg,
            "session_stats": {
                "discovered": self._markets_discovered,
                "entered": self._markets_entered,
                "filled": self._markets_filled,
                "both_filled": self._markets_both_filled,
                "unwound": self._markets_unwound,
                "no_fills": self._markets_no_fills,
                "realized_pnl": round(self._total_realized_pnl, 4),
                "capital_deployed": round(self._total_capital_deployed, 2),
            },
            "dry_run": self._dry_run,
            "payload": payload,
        }
        _write_line(JOURNAL_PATH, enriched)

    # ── Market lifecycle events ──────────────────────────────────────

    def market_discovered(self, slug: str, market_ts: int, title: str) -> None:
        self._markets_discovered += 1
        self._emit("market_discovered", {
            "slug": slug, "market_ts": market_ts, "title": title,
        })

    def market_deferred(self, slug: str, market_ts: int, details: dict) -> None:
        self._emit("market_deferred", {
            "slug": slug, "market_ts": market_ts, **details,
        })

    def market_expired(self, slug: str, market_ts: int, details: dict) -> None:
        self._emit("market_expired", {
            "slug": slug, "market_ts": market_ts, **details,
        })

    def edge_check(self, slug: str, market_ts: int,
                   combined_ask: float, edge: float,
                   book_snapshot: Optional[dict] = None) -> None:
        payload: dict = {
            "slug": slug, "market_ts": market_ts,
            "combined_ask": combined_ask, "edge": edge,
        }
        if book_snapshot:
            payload["book_snapshot"] = book_snapshot
        self._emit("edge_check", payload)

    def order_placed(self, slug: str, market_ts: int, side: str,
                     shares: float, price: float, order_id: str) -> None:
        cost = shares * price
        self._total_capital_deployed += cost
        self._emit("order_placed", {
            "slug": slug, "market_ts": market_ts, "side": side,
            "shares": shares, "price": price, "order_id": order_id,
            "cost": round(cost, 4),
        })

    def market_entered(self, slug: str, market_ts: int, title: str) -> None:
        self._markets_entered += 1
        self._emit("market_entered", {
            "slug": slug, "market_ts": market_ts, "title": title,
        })

    # ── Fill events ──────────────────────────────────────────────────

    def fill_detected(self, market_ts: int, side: str, delta: float,
                      total_matched: float, price: float) -> None:
        self._markets_filled += 1
        self._emit("fill_detected", {
            "market_ts": market_ts, "side": side,
            "delta": round(delta, 2), "total_matched": round(total_matched, 2),
            "price": price,
        })

    def one_sided_fill(self, market_ts: int, filled_side: str,
                       filled_shares: float) -> None:
        self._emit("one_sided_fill", {
            "market_ts": market_ts, "filled_side": filled_side,
            "filled_shares": round(filled_shares, 1),
        })

    def both_filled(self, market_ts: int, up_shares: float, dn_shares: float,
                    combined_vwap: float, hedged_profit: float) -> None:
        self._markets_both_filled += 1
        self._market_pnl[market_ts] = {
            "type": "both_filled",
            "hedged_profit": round(hedged_profit, 4),
            "combined_vwap": round(combined_vwap, 4),
        }
        self._emit("both_filled", {
            "market_ts": market_ts,
            "up_shares": up_shares, "dn_shares": dn_shares,
            "combined_vwap": round(combined_vwap, 4),
            "hedged_profit": round(hedged_profit, 2),
        })

    # ── Unwind events ────────────────────────────────────────────────

    def unwind_check(self, details: dict) -> None:
        self._emit("unwind_check", details)

    def unwind_started(self, market_ts: int, side_sold: str,
                       shares: float, sell_order_id: Optional[str]) -> None:
        self._emit("unwind_started", {
            "market_ts": market_ts, "side_sold": side_sold,
            "shares": round(shares, 1), "sell_order_id": sell_order_id,
        })

    def unwind_sold(self, market_ts: int, side_sold: str,
                    shares: float, sell_order_id: Optional[str],
                    exit_price: Optional[float] = None) -> None:
        self._markets_unwound += 1
        pnl = 0.0
        if exit_price is not None:
            entry_price = self._cfg.get("price", 0.45)
            fee = self._cfg.get("fee_per_share", 0.01)
            pnl = (exit_price - entry_price - fee) * shares
        self._total_realized_pnl += pnl
        self._market_pnl[market_ts] = {
            "type": "unwound",
            "exit_price": exit_price,
            "pnl": round(pnl, 4),
        }
        self._emit("unwind_sold", {
            "market_ts": market_ts, "side_sold": side_sold,
            "shares": round(shares, 1), "sell_order_id": sell_order_id,
            "exit_price": exit_price,
            "realized_pnl": round(pnl, 4),
        })

    # ── No-fill / cancel events ──────────────────────────────────────

    def market_no_fills(self, slug: str, market_ts: int) -> None:
        self._markets_no_fills += 1
        self._market_pnl[market_ts] = {"type": "no_fills", "pnl": 0.0}
        self._emit("market_no_fills", {"slug": slug, "market_ts": market_ts})

    def entry_cancelled_late(self, slug: str, market_ts: int,
                             time_remaining: int) -> None:
        self._markets_no_fills += 1
        self._emit("entry_cancelled_late", {
            "slug": slug, "market_ts": market_ts,
            "time_remaining": time_remaining,
        })

    # ── Price / redeem events ────────────────────────────────────────

    def price_update(self, market_ts: int, up_ask: float, dn_ask: float) -> None:
        self._emit("price_update", {
            "market_ts": market_ts, "up_ask": up_ask, "dn_ask": dn_ask,
        })

    def redeem_started(self, slug: str, market_ts: int,
                       up_bal: float, dn_bal: float) -> None:
        self._emit("redeem_started", {
            "slug": slug, "market_ts": market_ts,
            "up_bal": up_bal, "dn_bal": dn_bal,
        })

    def redeem_submitted(self, tx_id: str) -> None:
        self._emit("redeem_submitted", {"tx_id": tx_id})

    def redeem_confirmed(self, tx_id: str, tx_hash: str,
                         market_ts: Optional[int] = None,
                         payout: Optional[float] = None) -> None:
        if market_ts and payout is not None:
            entry_cost = self._cfg.get("price", 0.45) * 2 * self._cfg.get("shares_per_side", 5)
            pnl = payout - entry_cost
            self._total_realized_pnl += pnl
            self._market_pnl[market_ts] = {
                "type": "redeemed", "payout": payout, "pnl": round(pnl, 4),
            }
        self._emit("redeem_confirmed", {"tx_id": tx_id, "tx_hash": tx_hash})

    def redeem_failed(self, tx_id: str, state: str, error: str) -> None:
        self._emit("redeem_failed", {"tx_id": tx_id, "state": state, "error": error})

    def redeem_timeout(self, tx_id: str) -> None:
        self._emit("redeem_timeout", {"tx_id": tx_id})

    def redeem_error(self, error: str) -> None:
        self._emit("redeem_error", {"error": error})

    # ── Status / heartbeat ───────────────────────────────────────────

    def status(self, message: str, extra: Optional[dict] = None) -> None:
        payload: dict = {"message": message, "config": self._cfg, "dry_run": self._dry_run}
        if extra:
            payload.update(extra)
        self._emit("status", payload)

    def heartbeat(self, active_markets: int, watching: int) -> None:
        """Periodic summary emitted every N loop iterations."""
        self._heartbeat_counter += 1
        self._emit("heartbeat", {
            "tick": self._heartbeat_counter,
            "active_markets": active_markets,
            "watching": watching,
            "uptime_sec": int(time.time()) - self._session_start,
        })

    def session_summary(self) -> dict:
        """Return current session stats as a dict."""
        return {
            "session_start": self._session_start,
            "uptime_sec": int(time.time()) - self._session_start,
            "config_version": self._config_version,
            "config": self._cfg,
            "markets_discovered": self._markets_discovered,
            "markets_entered": self._markets_entered,
            "markets_filled": self._markets_filled,
            "markets_both_filled": self._markets_both_filled,
            "markets_unwound": self._markets_unwound,
            "markets_no_fills": self._markets_no_fills,
            "realized_pnl": round(self._total_realized_pnl, 4),
            "capital_deployed": round(self._total_capital_deployed, 2),
            "market_pnl": self._market_pnl,
        }
