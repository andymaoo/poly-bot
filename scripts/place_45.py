"""
EV-based BTC 15m Up/Down pair arbitrage bot (V2).

Algorithm:
- Market state machine: WATCHING -> ENTERED -> ONE_SIDED/BOTH_FILLED -> UNWOUND/DONE
- Dynamic entry gate with retry (no permanent skip)
- Fill monitoring via order status polling (size_matched)
- One-sided fill unwind: EV-based sell/wait decision
- VWAP position tracking per market

Usage:
  python scripts/place_45.py              # live trading
  python scripts/place_45.py --dry-run    # log decisions only, no real orders
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any, Dict

import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.bot.ws_book_feed import WSBookFeed
from src.bot.alerts import AlertManager
from scripts.trade_journal import TradeJournal, snap_book_levels

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("place45")

DRY_RUN = "--dry-run" in sys.argv


# ── Config ──────────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    price: float = 0.45
    shares_per_side: float = 60
    bail_price: float = 0.72
    max_combined_ask: float = 1.02
    min_ev_entry: float = 0.0
    order_expiry_seconds: int = 2700
    asset: str = "btc"
    bail_enabled: bool = False
    min_entry_time_remaining: int = 120
    unwind_hard_timeout: int = 90
    unwind_buffer: float = 0.01
    fee_per_share: float = 0.01


CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.jsonl")

GAMMA_URL = "https://gamma-api.polymarket.com"
REF_15M = 1771268400
MARKET_DURATION = 900
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def load_config() -> BotConfig:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = BotConfig()
    for fld in asdict(cfg).keys():
        if fld in data:
            setattr(cfg, fld, data[fld])
    return cfg


journal: TradeJournal | None = None
alerts: AlertManager | None = None


def emit_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Legacy fallback -- used only before journal is initialised."""
    event = {"type": event_type, "ts": int(time.time()), "payload": payload}
    try:
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        pass


# ── Market State Machine ────────────────────────────────────────────────

class MState(str, Enum):
    WATCHING = "watching"
    ENTERED = "entered"
    ONE_SIDED = "one_sided"
    BOTH_FILLED = "both_filled"
    UNWINDING = "unwinding"
    UNWOUND = "unwound"
    DONE = "done"


@dataclass
class PositionState:
    up_shares: float = 0.0
    dn_shares: float = 0.0
    up_cost: float = 0.0
    dn_cost: float = 0.0

    @property
    def up_vwap(self) -> float:
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0.0

    @property
    def dn_vwap(self) -> float:
        return self.dn_cost / self.dn_shares if self.dn_shares > 0 else 0.0

    @property
    def combined_vwap(self) -> float:
        return self.up_vwap + self.dn_vwap

    @property
    def hedged_shares(self) -> float:
        return min(self.up_shares, self.dn_shares)

    @property
    def hedged_profit(self) -> float:
        if self.hedged_shares <= 0:
            return 0.0
        return self.hedged_shares * (1.0 - self.combined_vwap)


@dataclass
class Market:
    ts: int
    slug: str
    up_token: str
    dn_token: str
    title: str
    condition_id: str
    neg_risk: bool
    state: MState = MState.WATCHING
    up_order_id: str | None = None
    dn_order_id: str | None = None
    position: PositionState = field(default_factory=PositionState)
    entry_time: float = 0.0
    one_sided_since: float = 0.0
    filled_side: str = ""
    deferred_count: int = 0
    redeemed: bool = False
    last_up_matched: float = 0.0
    last_dn_matched: float = 0.0
    up_limit_price: float = 0.0
    dn_limit_price: float = 0.0
    unwind_order_id: str | None = None
    last_unwind_matched: float = 0.0
    unwind_target_shares: float = 0.0
    unwind_entry_vwap: float = 0.0


# ── SDK Init ────────────────────────────────────────────────────────────

class _PriceLevel:
    def __init__(self, price: float, size: float):
        self.price = price
        self.size = size


class _OrderBook:
    def __init__(self, asks: list, bids: list):
        self.asks = asks
        self.bids = bids


class MockClobClient:
    """Mock CLOB client for dry-run when no credentials. Returns plausible fake books."""

    def get_order_book(self, token_id: str):
        # Fake book: up_ask ~0.48, dn_ask ~0.48 -> combined 0.96, edge 0.04
        return _OrderBook(
            asks=[_PriceLevel(0.48, 25), _PriceLevel(0.49, 15), _PriceLevel(0.50, 10)],
            bids=[_PriceLevel(0.46, 20), _PriceLevel(0.45, 30), _PriceLevel(0.44, 15)],
        )

    def get_orders(self):
        return []

    def get_order(self, order_id: str):
        return {"size_matched": 0}


def init_clob_client():
    if DRY_RUN and not os.getenv("POLYMARKET_PRIVATE_KEY"):
        log.info("Using mock CLOB client (no credentials, dry-run)")
        return MockClobClient()

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.getenv("POLYMARKET_FUNDER", "")
    kwargs = {"funder": funder} if funder else {}
    client = ClobClient(
        "https://clob.polymarket.com", chain_id=137, key=pk,
        signature_type=1, **kwargs,
    )
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    api_secret = os.getenv("POLYMARKET_API_SECRET", "")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")
    if api_key and api_secret and passphrase:
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
    else:
        creds = client.derive_api_key()
        log.info("Derived API creds from private key")
    client.set_api_creds(creds)
    return client


def init_relayer():
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

    bk = os.getenv("POLYMARKET_BUILDER_API_KEY", "")
    bs = os.getenv("POLYMARKET_BUILDER_SECRET", "")
    bp = os.getenv("POLYMARKET_BUILDER_PASSPHRASE", "")
    if not (bk and bs and bp):
        log.warning("No builder creds - auto-redeem disabled")
        return None
    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(key=bk, secret=bs, passphrase=bp)
    )
    return RelayClient(
        relayer_url="https://relayer-v2.polymarket.com",
        chain_id=137,
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        builder_config=builder_config,
    )


# ── Market Discovery ────────────────────────────────────────────────────

def next_market_timestamps(now_ts: int) -> list[int]:
    """Return [previous, current, next, next+1] 15-min slot timestamps.
    Includes the market 30–45 min out so we can place orders earlier."""
    ts = REF_15M
    while ts < now_ts:
        ts += 900
    return [ts - 900, ts, ts + 900, ts + 1800]


async def get_market_info(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{GAMMA_URL}/events", params={"slug": slug})
        r.raise_for_status()
        data = r.json()
    if not data:
        raise ValueError(f"No event found for slug: {slug}")
    m = data[0]["markets"][0]
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
    return {
        "up_token": tokens[0],
        "dn_token": tokens[1],
        "title": m.get("question", slug),
        "conditionId": m.get("conditionId", ""),
        "closed": m.get("closed", False),
        "neg_risk": m.get("negRisk", False),
    }


# ── Order Placement / Cancel / Sell ─────────────────────────────────────

def place_order(client, token_id: str, side_label: str, shares: float,
                price: float, expiration_s: int) -> str | None:
    if DRY_RUN:
        log.info("  [DRY] %s BUY %.0f @ %.2f", side_label, shares, price)
        return f"dry-{side_label.strip().lower()}-{int(time.time())}"

    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType

    order_args = OrderArgs(
        token_id=token_id, price=price, size=round(shares, 1),
        side=BUY, expiration=int(time.time()) + int(expiration_s),
    )
    try:
        signed = client.create_order(order_args)
        result = client.post_order(signed, OrderType.GTD)
        oid = result.get("orderID", result.get("id", ""))
        if oid:
            log.info("  %s BUY %.0f @ %.2f  [%s]", side_label, shares, price, oid[:12])
            return oid
        else:
            log.warning("  %s no order ID: %s", side_label, result)
    except Exception as e:
        log.error("  %s order failed: %s", side_label, e)
    return None


def sell_at_bid(client, token_id: str, shares: float, side_label: str,
                expiration_s: int = 300) -> str | None:
    if DRY_RUN:
        log.info("  [DRY] %s SELL %.0f (at best bid)", side_label, shares)
        return f"dry-sell-{side_label.strip().lower()}-{int(time.time())}"

    from py_clob_client.order_builder.constants import SELL
    from py_clob_client.clob_types import OrderArgs, OrderType

    try:
        book = client.get_order_book(token_id)
        if not book.bids:
            log.warning("  %s no bids to sell into", side_label)
            return None
        best_bid = float(book.bids[0].price)
        log.info("  %s SELL %.0f @ %.2f (best bid)", side_label, shares, best_bid)
        order_args = OrderArgs(
            token_id=token_id, price=best_bid, size=round(shares, 1),
            side=SELL, expiration=int(time.time()) + int(expiration_s),
        )
        signed = client.create_order(order_args)
        result = client.post_order(signed, OrderType.GTD)
        oid = result.get("orderID", result.get("id", ""))
        if oid:
            log.info("  %s SELL placed [%s]", side_label, oid[:12])
            return oid
        else:
            log.warning("  %s SELL no order ID: %s", side_label, result)
    except Exception as e:
        log.error("  %s SELL failed: %s", side_label, e)
    return None


def cancel_market_orders(client, token_ids: set):
    if DRY_RUN:
        log.info("  [DRY] Cancel orders for %d token(s)", len(token_ids))
        return
    try:
        orders = client.get_orders()
        to_cancel = [o["id"] for o in orders
                     if o.get("status") == "LIVE"
                     and o.get("asset_id") in token_ids
                     and "id" in o]
        if to_cancel:
            client.cancel_orders(to_cancel)
            log.info("  Cancelled %d order(s)", len(to_cancel))
    except Exception as e:
        log.error("  Cancel failed: %s", e)


def cancel_order_by_id(client, order_id: str) -> bool:
    """Cancel a single order by ID."""
    if DRY_RUN:
        return True
    try:
        client.cancel_orders([order_id])
        return True
    except Exception as e:
        log.error("  Cancel order %s failed: %s", order_id[:12], e)
        return False


# ── Balance / Fill Checks ───────────────────────────────────────────────

def check_token_balance(client, token_id: str) -> float:
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    try:
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id, signature_type=2,
            )
        )
        return int(bal.get("balance", "0")) / 1e6
    except Exception:
        return 0.0


def get_order_fill(client, order_id: str) -> float:
    """Return size_matched for an order."""
    if DRY_RUN:
        return 0.0
    try:
        order = client.get_order(order_id)
        return float(order.get("size_matched", 0) or 0)
    except Exception:
        return 0.0


# ── Redeem ──────────────────────────────────────────────────────────────

def redeem_market(relayer, condition_id: str, neg_risk: bool) -> bool:
    from web3 import Web3
    from eth_abi import encode
    from py_builder_relayer_client.models import SafeTransaction, OperationType

    if neg_risk:
        NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
        cond_bytes = bytes.fromhex(
            condition_id[2:] if condition_id.startswith("0x") else condition_id
        )
        max_uint = 2**256 - 1
        selector = Web3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
        params = encode(
            ["bytes32", "uint256[]"], [cond_bytes, [max_uint, max_uint]]
        )
        target = NEG_RISK_ADAPTER
    else:
        cond_bytes = bytes.fromhex(
            condition_id[2:] if condition_id.startswith("0x") else condition_id
        )
        selector = Web3.keccak(
            text="redeemPositions(address,bytes32,bytes32,uint256[])"
        )[:4]
        params = encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_ADDRESS, b"\x00" * 32, cond_bytes, [1, 2]],
        )
        target = CTF_ADDRESS

    calldata = "0x" + (selector + params).hex()
    tx = SafeTransaction(
        to=target, operation=OperationType.Call, data=calldata, value="0"
    )

    try:
        resp = relayer.execute([tx], "Redeem positions")
        tx_id = getattr(resp, "transaction_id", None) or getattr(resp, "id", None)
        log.info("  Redeem submitted: %s", tx_id)
        if journal:
            journal.redeem_submitted(tx_id)

        for _ in range(20):
            time.sleep(3)
            status = relayer.get_transaction(tx_id)
            if isinstance(status, list):
                status = status[0]
            state = status.get("state", "")
            if "CONFIRMED" in state:
                tx_hash = status.get("transactionHash", "")
                log.info("  Redeem CONFIRMED: %s", tx_hash[:20])
                if journal:
                    journal.redeem_confirmed(tx_id, tx_hash)
                return True
            if "FAILED" in state or "INVALID" in state:
                msg = status.get("errorMsg", "")[:80]
                log.error("  Redeem FAILED: %s", msg)
                if journal:
                    journal.redeem_failed(tx_id, state, msg)
                return False
        log.warning("  Redeem timeout - check manually")
        if journal:
            journal.redeem_timeout(tx_id)
        return False
    except Exception as e:
        log.error("  Redeem error: %s", e)
        if journal:
            journal.redeem_error(str(e))
        return False


# ── Entry Gate (dynamic, retryable) ─────────────────────────────────────

def evaluate_entry(client, mkt: Market, cfg: BotConfig) -> tuple[bool, dict]:
    """Returns (should_enter, details). Soft rejections are retried next loop.

    In live mode this uses a simple EV heuristic based on combined ask and depth.
    In dry-run, we still evaluate EV but avoid skipping purely on EV so that the
    logs can be used to tune the gate.
    """
    now = int(time.time())
    time_remaining = (mkt.ts + MARKET_DURATION) - now

    # Hard time gate: never enter when too close to expiry.
    if time_remaining < cfg.min_entry_time_remaining:
        return False, {
            "reject": "hard", "reason": "too_late",
            "time_remaining": time_remaining,
        }

    try:
        up_book = client.get_order_book(mkt.up_token)
        dn_book = client.get_order_book(mkt.dn_token)
        up_best_ask = min(float(a.price) for a in up_book.asks) if up_book.asks else None
        dn_best_ask = min(float(a.price) for a in dn_book.asks) if dn_book.asks else None

        book_snapshot = {
            "up_asks": snap_book_levels(up_book, "asks", 3),
            "up_bids": snap_book_levels(up_book, "bids", 3),
            "dn_asks": snap_book_levels(dn_book, "asks", 3),
            "dn_bids": snap_book_levels(dn_book, "bids", 3),
        }

        if up_best_ask is None or dn_best_ask is None:
            return False, {"reject": "soft", "reason": "no_book_data",
                           "book_snapshot": book_snapshot}

        combined_ask = up_best_ask + dn_best_ask
        edge = 1.0 - combined_ask

        # --- Entry gates commented out for 1-side sell/hold testing ---
        # if combined_ask >= cfg.max_combined_ask:
        #     return False, {
        #         "reject": "soft", "reason": "no_edge",
        #         "combined_ask": round(combined_ask, 4), "edge": round(edge, 4),
        #         "book_snapshot": book_snapshot,
        #     }

        up_depth = sum(float(a.size) for a in up_book.asks[:3]) if up_book.asks else 0
        dn_depth = sum(float(a.size) for a in dn_book.asks[:3]) if dn_book.asks else 0
        # if up_depth < 10 or dn_depth < 10:
        #     return False, {
        #         "reject": "soft", "reason": "thin_book",
        #         "up_depth": round(up_depth, 1), "dn_depth": round(dn_depth, 1),
        #         "book_snapshot": book_snapshot,
        #     }

        # --- EV-based gate (heuristic) - commented out for testing ---
        # Approximate per-pair profit if both legs fill at the entry price.
        f_entry = cfg.fee_per_share
        pi_pair = 1.0 - cfg.price - cfg.price - 2 * f_entry

        # Heuristic probabilities based on edge and depth.
        base_edge = max(edge, 0.0)
        depth_score = min(1.0, (up_depth + dn_depth) / 40.0)  # 40 shares across top 3 each is "good".
        p_both = max(0.0, min(1.0, base_edge * 8.0 * depth_score))
        p_one = 0.25 * p_both
        p_none = max(0.0, 1.0 - p_both - p_one)

        # Approximate EV of one-sided unwind if it occurs.
        ev_unwind = base_edge * 0.5

        ev_entry = p_both * pi_pair + p_one * ev_unwind
        min_ev = cfg.min_ev_entry

        # EV gate commented out for 1-side sell/hold testing
        # if not DRY_RUN and ev_entry <= min_ev:
        #     return False, {
        #         "reject": "soft", "reason": "negative_ev",
        #         "combined_ask": round(combined_ask, 4), "edge": round(edge, 4),
        #         "ev_entry": round(ev_entry, 4), "min_ev_entry": round(min_ev, 4),
        #         "p_both": round(p_both, 4), "p_one": round(p_one, 4), "p_none": round(p_none, 4),
        #         "book_snapshot": book_snapshot,
        #     }

        return True, {
            "combined_ask": round(combined_ask, 4), "edge": round(edge, 4),
            "up_depth": round(up_depth, 1), "dn_depth": round(dn_depth, 1),
            "ev_entry": round(ev_entry, 4), "min_ev_entry": round(min_ev, 4),
            "p_both": round(p_both, 4), "p_one": round(p_one, 4), "p_none": round(p_none, 4),
            "book_snapshot": book_snapshot,
        }
    except Exception as e:
        log.warning("  Entry gate error: %s (proceeding anyway)", e)
        return True, {"reason": "gate_error", "error": str(e)}


# ── Fill Monitoring ─────────────────────────────────────────────────────

def check_fills(client, mkt: Market, cfg: BotConfig) -> None:
    """Poll order status and update market position / state."""
    if DRY_RUN:
        return

    for side, oid_attr, matched_attr in [
        ("UP", "up_order_id", "last_up_matched"),
        ("DN", "dn_order_id", "last_dn_matched"),
    ]:
        oid = getattr(mkt, oid_attr)
        if not oid:
            continue

        new_matched = get_order_fill(client, oid)
        old_matched = getattr(mkt, matched_attr)
        delta = new_matched - old_matched

        if delta > 0.001:
            price = mkt.up_limit_price if side == "UP" else mkt.dn_limit_price
            if side == "UP":
                mkt.position.up_shares += delta
                mkt.position.up_cost += delta * price
            else:
                mkt.position.dn_shares += delta
                mkt.position.dn_cost += delta * price

            setattr(mkt, matched_attr, new_matched)
            total = mkt.position.up_shares if side == "UP" else mkt.position.dn_shares
            log.info("  FILL: %s +%.1f shares (total: %.1f)", side, delta, total)
            if journal:
                journal.fill_detected(mkt.ts, side, delta, new_matched, price)

    up_filled = mkt.position.up_shares > 0
    dn_filled = mkt.position.dn_shares > 0

    if up_filled and dn_filled and mkt.state == MState.ENTERED:
        mkt.state = MState.BOTH_FILLED
        log.info("  BOTH sides filled! hedged_profit=$%.2f", mkt.position.hedged_profit)
        if journal:
            journal.both_filled(mkt.ts, mkt.position.up_shares,
                                mkt.position.dn_shares,
                                mkt.position.combined_vwap,
                                mkt.position.hedged_profit)
        if alerts:
            asyncio.get_running_loop().create_task(alerts.v2_fill(
                mkt.slug, "BOTH", mkt.position.hedged_shares,
                0, True, mkt.position.hedged_profit,
            ))
    elif (up_filled != dn_filled) and mkt.state == MState.ENTERED:
        mkt.state = MState.ONE_SIDED
        mkt.filled_side = "UP" if up_filled else "DOWN"
        mkt.one_sided_since = time.time()
        filled_shares = mkt.position.up_shares if up_filled else mkt.position.dn_shares
        log.info("  ONE-SIDED: %s filled (%.1f), %s unfilled",
                 mkt.filled_side, filled_shares,
                 "DOWN" if up_filled else "UP")
        if journal:
            journal.one_sided_fill(mkt.ts, mkt.filled_side, filled_shares)
        if alerts:
            price = mkt.up_limit_price if up_filled else mkt.dn_limit_price
            asyncio.get_running_loop().create_task(alerts.v2_fill(
                mkt.slug, mkt.filled_side, filled_shares, price, False,
            ))


# ── Unwind Logic (EV-based sell/wait) ──────────────────────────────────

def evaluate_unwind(mkt: Market, ws_feed: WSBookFeed,
                    cfg: BotConfig) -> tuple[bool, dict]:
    """Decide whether to sell the filled side using a multi-horizon EV heuristic."""
    now = time.time()
    time_remaining = (mkt.ts + MARKET_DURATION) - now

    is_up = mkt.filled_side == "UP"
    filled_shares = mkt.position.up_shares if is_up else mkt.position.dn_shares
    a = mkt.position.up_vwap if is_up else mkt.position.dn_vwap
    filled_token = mkt.up_token if is_up else mkt.dn_token

    s_now = ws_feed.get_best_bid(filled_token)
    if s_now is None:
        return False, {"reason": "no_bid_data", "market_ts": mkt.ts}

    b = cfg.price
    f_entry = cfg.fee_per_share
    f_exit = cfg.fee_per_share

    pi_exit_now = s_now - a - f_exit
    pi_pair = 1.0 - a - b - 2 * f_entry

    unfilled_token = mkt.dn_token if is_up else mkt.up_token
    opp_ask = ws_feed.get_best_ask(unfilled_token)

    def heuristic_p_fill(delta: int, time_rem: float) -> float:
        remaining_frac = max(0.0, min(1.0, time_rem / MARKET_DURATION))
        base = max(0.05, 0.4 * (remaining_frac ** 2))
        # Longer wait windows slightly increase fill chance.
        scale = min(1.5, max(0.5, delta / 10.0))
        p = base * scale
        if opp_ask is not None and abs(opp_ask - cfg.price) <= 0.02:
            p *= 1.5
        return max(0.05, min(0.9, p))

    horizons = [3, 5, 8, 12, 20, 30]
    best_ev = float("-inf")
    best_delta = 0
    best_p_fill = 0.0

    for delta in horizons:
        if time_remaining <= 0:
            break
        p_fill = heuristic_p_fill(delta, time_remaining)
        # Later exits are assumed to be slightly worse than current exit.
        risk_discount = 0.01 + 0.0005 * delta
        pi_late_exit = pi_exit_now - risk_discount
        ev = p_fill * pi_pair + (1.0 - p_fill) * pi_late_exit
        if ev > best_ev:
            best_ev = ev
            best_delta = delta
            best_p_fill = p_fill

    if best_delta == 0:
        best_delta = horizons[0]
        best_ev = pi_exit_now
        best_p_fill = 0.0

    details = {
        "market_ts": mkt.ts, "filled_side": mkt.filled_side,
        "s_now": round(s_now, 4), "a": round(a, 4),
        "pi_exit_now": round(pi_exit_now, 4), "pi_pair": round(pi_pair, 4),
        "p_fill": round(best_p_fill, 4), "ev_wait": round(best_ev, 4),
        "best_delta": best_delta,
        "time_remaining": round(time_remaining, 0),
        "filled_shares": round(filled_shares, 1),
    }

    if pi_exit_now >= pi_pair:
        details["trigger"] = "profit_exceeds_pair"
        return True, details

    if best_ev <= pi_exit_now + cfg.unwind_buffer:
        details["trigger"] = "ev_wait_low"
        return True, details

    if time_remaining < cfg.unwind_hard_timeout:
        details["trigger"] = "hard_timeout"
        return True, details

    return False, details


def execute_unwind(client, mkt: Market, cfg: BotConfig) -> None:
    is_up = mkt.filled_side == "UP"
    filled_shares = mkt.position.up_shares if is_up else mkt.position.dn_shares
    filled_token = mkt.up_token if is_up else mkt.dn_token
    side_label = "UP" if is_up else "DN"

    log.info("  UNWIND: selling %s %.1f shares", side_label, filled_shares)
    cancel_market_orders(client, {mkt.up_token, mkt.dn_token})

    if DRY_RUN:
        sell_oid = sell_at_bid(
            client, filled_token, filled_shares, side_label,
            expiration_s=cfg.order_expiry_seconds,
        )
        mkt.state = MState.UNWOUND
        if journal:
            journal.unwind_sold(mkt.ts, mkt.filled_side, filled_shares, sell_oid)
        return

    mkt.unwind_entry_vwap = mkt.position.up_vwap if is_up else mkt.position.dn_vwap
    sell_oid = sell_at_bid(
        client, filled_token, filled_shares, side_label,
        expiration_s=cfg.order_expiry_seconds,
    )
    mkt.unwind_order_id = sell_oid
    mkt.unwind_target_shares = filled_shares
    mkt.last_unwind_matched = 0.0
    mkt.state = MState.UNWINDING
    if journal:
        journal.unwind_started(mkt.ts, mkt.filled_side, filled_shares, sell_oid)


# ── Redeem All ──────────────────────────────────────────────────────────

async def try_redeem_all(client, relayer, markets: dict, cfg: BotConfig):
    if not relayer:
        return

    for ts, mkt in list(markets.items()):
        if mkt.redeemed or mkt.state in (MState.UNWOUND, MState.UNWINDING):
            continue
        if mkt.state not in (MState.BOTH_FILLED, MState.DONE, MState.ENTERED):
            continue

        try:
            info = await get_market_info(mkt.slug)
        except Exception:
            continue
        if not info["closed"]:
            continue

        up_bal = check_token_balance(client, mkt.up_token) if not DRY_RUN else 0
        dn_bal = check_token_balance(client, mkt.dn_token) if not DRY_RUN else 0

        if up_bal <= 0 and dn_bal <= 0:
            mkt.redeemed = True
            mkt.state = MState.DONE
            continue

        log.info("  Redeeming %s (UP=%.1f, DN=%.1f)...", mkt.title[:50], up_bal, dn_bal)
        journal.redeem_started(mkt.slug, ts, up_bal, dn_bal)

        if DRY_RUN:
            log.info("  [DRY] Would redeem %s", mkt.slug)
            mkt.redeemed = True
            mkt.state = MState.DONE
        else:
            ok = redeem_market(relayer, mkt.condition_id, mkt.neg_risk)
            if ok:
                mkt.redeemed = True
                mkt.state = MState.DONE


# ── Main Loop ───────────────────────────────────────────────────────────

async def run():
    global journal, alerts
    cfg = load_config()
    journal = TradeJournal(asdict(cfg), dry_run=DRY_RUN)
    journal.status("bot_starting")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    alerts = AlertManager(bot_token=tg_token, chat_id=tg_chat)

    if DRY_RUN:
        log.info("*** DRY RUN MODE - no real orders will be placed ***\n")

    log.info("Initializing SDK...")
    client = init_clob_client()
    relayer = init_relayer()
    log.info(
        "SDK ready. Placing %.2fc orders on %s 15m markets (%.0f shares/side).%s",
        cfg.price, cfg.asset.upper(), cfg.shares_per_side,
        " [DRY RUN]" if DRY_RUN else "",
    )
    if relayer:
        log.info("Auto-redeem enabled (gasless relayer).\n")
    else:
        log.info("Auto-redeem DISABLED (no builder creds).\n")

    ws_feed = WSBookFeed()
    markets: dict[int, Market] = {}

    now = int(time.time())
    log.info("Scanning recent markets for unredeemed positions...")
    scan_ts = REF_15M
    while scan_ts < now - 7200:
        scan_ts += 900
    while scan_ts < now:
        slug = f"{cfg.asset}-updown-15m-{scan_ts}"
        try:
            info = await get_market_info(slug)
            markets[scan_ts] = Market(
                ts=scan_ts, slug=slug,
                up_token=info["up_token"], dn_token=info["dn_token"],
                title=info["title"], condition_id=info["conditionId"],
                neg_risk=info["neg_risk"], state=MState.DONE,
            )
        except Exception:
            pass
        scan_ts += 900
    log.info("Found %d recent markets to track for redemption.\n", len(markets))

    loop_count = 0
    while True:
        now = int(time.time())
        cfg = load_config()
        journal.update_config(asdict(cfg))
        timestamps = next_market_timestamps(now)
        loop_count += 1

        # Phase 1: discover new markets
        for ts in timestamps:
            if ts in markets:
                continue
            slug = f"{cfg.asset}-updown-15m-{ts}"
            try:
                info = await get_market_info(slug)
            except Exception:
                continue

            mkt = Market(
                ts=ts, slug=slug,
                up_token=info["up_token"], dn_token=info["dn_token"],
                title=info["title"], condition_id=info["conditionId"],
                neg_risk=info["neg_risk"], state=MState.WATCHING,
            )
            markets[ts] = mkt
            log.info("=" * 60)
            log.info("DISCOVERED: %s", mkt.title)
            log.info("  Slug: %s  | State: WATCHING", slug)
            journal.market_discovered(slug, ts, mkt.title)

            if not ws_feed.is_connected:
                await ws_feed.start([mkt.up_token, mkt.dn_token])
            else:
                await ws_feed.subscribe([mkt.up_token, mkt.dn_token])

        # Phase 2: process each market by state
        for ts, mkt in list(markets.items()):

            # ── WATCHING: evaluate entry ──
            if mkt.state == MState.WATCHING:
                if not DRY_RUN:
                    up_bal = check_token_balance(client, mkt.up_token)
                    dn_bal = check_token_balance(client, mkt.dn_token)
                else:
                    up_bal = dn_bal = 0.0

                if up_bal > 0 or dn_bal > 0:
                    log.info("  [%s] Existing position (UP=%.1f, DN=%.1f)",
                             mkt.slug, up_bal, dn_bal)
                    mkt.position.up_shares = up_bal
                    mkt.position.dn_shares = dn_bal
                    mkt.position.up_cost = up_bal * cfg.price if up_bal > 0 else 0.0
                    mkt.position.dn_cost = dn_bal * cfg.price if dn_bal > 0 else 0.0
                    mkt.up_limit_price = cfg.price
                    mkt.dn_limit_price = cfg.price
                    if up_bal > 0 and dn_bal > 0:
                        mkt.state = MState.BOTH_FILLED
                    else:
                        mkt.state = MState.ONE_SIDED
                        mkt.filled_side = "UP" if up_bal > 0 else "DOWN"
                        mkt.one_sided_since = time.time()
                    continue

                # Cancel any existing live orders for this market so we place fresh ones.
                # (Previously we skipped to ENTERED; now we always evaluate and place.)
                if not DRY_RUN:
                    try:
                        live_orders = client.get_orders()
                        tokens = {mkt.up_token, mkt.dn_token}
                        existing = [o for o in live_orders
                                    if o.get("status") == "LIVE"
                                    and o.get("asset_id") in tokens]
                        if existing:
                            log.info("  [%s] Cancelling %d stale order(s), placing fresh",
                                     mkt.slug, len(existing))
                            cancel_market_orders(client, tokens)
                    except Exception:
                        pass

                should_enter, details = evaluate_entry(client, mkt, cfg)

                if details.get("reject") == "hard":
                    log.info("  [%s] EXPIRED: %s (%ds left)",
                             mkt.slug, details.get("reason"),
                             details.get("time_remaining", 0))
                    mkt.state = MState.DONE
                    journal.market_expired(mkt.slug, ts, details)
                    continue

                if not should_enter:
                    mkt.deferred_count += 1
                    if mkt.deferred_count <= 3 or mkt.deferred_count % 6 == 0:
                        log.info("  [%s] DEFERRED (%dx): %s (ask=%.3f)",
                                 mkt.slug, mkt.deferred_count,
                                 details.get("reason", "?"),
                                 details.get("combined_ask", 0))
                    journal.market_deferred(mkt.slug, ts, details)
                    continue

                log.info("  [%s] ENTERING (edge=%.3f)", mkt.slug,
                         details.get("edge", 0))
                journal.edge_check(
                    mkt.slug, ts,
                    combined_ask=details.get("combined_ask", 0),
                    edge=details.get("edge", 0),
                    book_snapshot=details.get("book_snapshot"),
                )

                up_id = place_order(
                    client, mkt.up_token, "UP  ",
                    cfg.shares_per_side, cfg.price, cfg.order_expiry_seconds,
                )
                if up_id:
                    mkt.up_order_id = up_id
                    mkt.up_limit_price = cfg.price
                    journal.order_placed(mkt.slug, ts, "UP",
                                         cfg.shares_per_side, cfg.price, up_id)

                dn_id = place_order(
                    client, mkt.dn_token, "DOWN",
                    cfg.shares_per_side, cfg.price, cfg.order_expiry_seconds,
                )
                if dn_id:
                    mkt.dn_order_id = dn_id
                    mkt.dn_limit_price = cfg.price
                    journal.order_placed(mkt.slug, ts, "DOWN",
                                         cfg.shares_per_side, cfg.price, dn_id)

                placed = (1 if up_id else 0) + (1 if dn_id else 0)
                log.info("  %d orders placed. Max cost: $%.2f",
                         placed, cfg.shares_per_side * cfg.price * 2)

                mkt.state = MState.ENTERED
                mkt.entry_time = time.time()
                journal.market_entered(mkt.slug, ts, mkt.title)
                if alerts:
                    await alerts.v2_entry(
                        mkt.slug, details.get("edge", 0),
                        details.get("combined_ask", 0),
                        cfg.price, cfg.shares_per_side,
                        details.get("ev_entry", 0),
                    )

            # ── ENTERED: monitor fills ──
            elif mkt.state == MState.ENTERED:
                check_fills(client, mkt, cfg)

                time_remaining = (mkt.ts + MARKET_DURATION) - now
                no_fills = (mkt.position.up_shares == 0
                            and mkt.position.dn_shares == 0)
                if time_remaining < 0 and no_fills:
                    log.info("  [%s] Market ended with no fills", mkt.slug)
                    mkt.state = MState.DONE
                    journal.market_no_fills(mkt.slug, ts)
                elif time_remaining < 60 and no_fills and (mkt.up_order_id or mkt.dn_order_id):
                    log.info("  [%s] Late cancel of entry orders (no fills, %ds left)",
                             mkt.slug, int(time_remaining))
                    cancel_market_orders(client, {mkt.up_token, mkt.dn_token})
                    mkt.state = MState.DONE
                    journal.entry_cancelled_late(mkt.slug, ts, int(time_remaining))

            # ── ONE_SIDED: unwind evaluation ──
            elif mkt.state == MState.ONE_SIDED:
                check_fills(client, mkt, cfg)
                if mkt.state == MState.BOTH_FILLED:
                    continue

                should_sell, details = evaluate_unwind(mkt, ws_feed, cfg)
                journal.unwind_check(details)

                if should_sell:
                    log.info(
                        "  [%s] UNWIND: %s | exit=%.4f ev_wait=%.4f p=%.3f",
                        mkt.slug, details.get("trigger"),
                        details.get("pi_exit_now", 0),
                        details.get("ev_wait", 0),
                        details.get("p_fill", 0),
                    )
                    execute_unwind(client, mkt, cfg)
                    if alerts:
                        s_now = details.get("s_now", 0)
                        a = details.get("a", 0)
                        fee = cfg.fee_per_share
                        pnl = (s_now - a - fee) * details.get("filled_shares", 0)
                        await alerts.v2_unwind(
                            mkt.slug, details.get("trigger", ""),
                            mkt.filled_side, details.get("filled_shares", 0),
                            s_now, pnl,
                        )
                else:
                    log.info(
                        "  [%s] WAIT: ev=%.4f > exit=%.4f (p=%.3f, %ds left)",
                        mkt.slug,
                        details.get("ev_wait", 0),
                        details.get("pi_exit_now", 0),
                        details.get("p_fill", 0),
                        details.get("time_remaining", 0),
                    )

            # ── UNWINDING: poll unwind sell order until fully filled ──
            elif mkt.state == MState.UNWINDING:
                if not mkt.unwind_order_id:
                    mkt.state = MState.UNWOUND
                    continue
                time_remaining = (mkt.ts + MARKET_DURATION) - now
                is_up = mkt.filled_side == "UP"
                filled_token = mkt.up_token if is_up else mkt.dn_token
                side_label = "UP" if is_up else "DN"

                new_matched = get_order_fill(client, mkt.unwind_order_id)
                delta = new_matched - mkt.last_unwind_matched

                if delta > 0.001:
                    mkt.last_unwind_matched = new_matched
                    if is_up:
                        if mkt.position.up_shares > 0:
                            cost_ratio = mkt.position.up_cost / mkt.position.up_shares
                            mkt.position.up_cost -= delta * cost_ratio
                        mkt.position.up_shares = max(0.0, mkt.position.up_shares - delta)
                    else:
                        if mkt.position.dn_shares > 0:
                            cost_ratio = mkt.position.dn_cost / mkt.position.dn_shares
                            mkt.position.dn_cost -= delta * cost_ratio
                        mkt.position.dn_shares = max(0.0, mkt.position.dn_shares - delta)
                    log.info("  [%s] UNWIND fill: %s +%.1f (total matched: %.1f)",
                             mkt.slug, side_label, delta, new_matched)

                if new_matched >= mkt.unwind_target_shares - 0.001:
                    target_shares = mkt.unwind_target_shares
                    a = mkt.unwind_entry_vwap
                    oid_cleared = mkt.unwind_order_id
                    mkt.state = MState.UNWOUND
                    if is_up:
                        mkt.position.up_shares = 0.0
                        mkt.position.up_cost = 0.0
                    else:
                        mkt.position.dn_shares = 0.0
                        mkt.position.dn_cost = 0.0
                    exit_price = ws_feed.get_best_bid(filled_token) if ws_feed else None
                    if journal:
                        journal.unwind_sold(
                            mkt.ts, mkt.filled_side, target_shares,
                            oid_cleared, exit_price=exit_price,
                        )
                    mkt.unwind_order_id = None
                    mkt.unwind_target_shares = 0.0
                    mkt.last_unwind_matched = 0.0
                    if alerts:
                        fee = cfg.fee_per_share
                        pnl = (exit_price - a - fee) * target_shares if exit_price else 0.0
                        await alerts.v2_unwind(
                            mkt.slug, "unwind_filled", mkt.filled_side,
                            target_shares, exit_price or 0.0, pnl,
                        )
                    log.info("  [%s] UNWOUND (sell order %s fully filled)", mkt.slug, oid_cleared[:12] if oid_cleared else "?")

                elif time_remaining < 60 and new_matched < mkt.unwind_target_shares - 0.001:
                    remaining = mkt.unwind_target_shares - new_matched
                    if cancel_order_by_id(client, mkt.unwind_order_id):
                        new_oid = sell_at_bid(
                            client, filled_token, remaining, side_label,
                            expiration_s=cfg.order_expiry_seconds,
                        )
                        if new_oid:
                            mkt.unwind_order_id = new_oid
                            mkt.unwind_target_shares = remaining
                            mkt.last_unwind_matched = 0.0
                            log.info("  [%s] UNWIND replace (stale, %ds left, %.1f shares)",
                                     mkt.slug, int(time_remaining), remaining)

            # ── BOTH_FILLED / UNWOUND / DONE: handled by redeem or no-op ──

        # Phase 3: emit WS price updates for active markets
        for ts, mkt in markets.items():
            if mkt.state in (MState.DONE, MState.UNWOUND, MState.UNWINDING):
                continue
            up_ask = ws_feed.get_best_ask(mkt.up_token)
            dn_ask = ws_feed.get_best_ask(mkt.dn_token)
            if up_ask is not None or dn_ask is not None:
                journal.price_update(ts, up_ask or 0, dn_ask or 0)

        # Phase 4: redeem resolved markets
        await try_redeem_all(client, relayer, markets, cfg)

        # Phase 5: cleanup old markets (keep 2h for redemption)
        markets = {ts: m for ts, m in markets.items() if ts > now - 7200}

        # Phase 6: periodic heartbeat (every ~60s)
        if loop_count % 6 == 0:
            active = sum(1 for m in markets.values()
                         if m.state not in (MState.DONE, MState.UNWOUND))
            watching = sum(1 for m in markets.values()
                          if m.state == MState.WATCHING)
            journal.heartbeat(active, watching)

        # Phase 7: Telegram heartbeat (every ~5min) + kill check
        if alerts and loop_count % 30 == 0:
            stats = journal.session_summary()
            await alerts.v2_heartbeat(stats)
        if alerts and loop_count % 6 == 0:
            if await alerts.check_kill_command():
                log.info("Kill command received - shutting down")
                journal.status("kill_command_received")
                break

        await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("\nStopped.")
