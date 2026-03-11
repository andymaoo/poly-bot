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


def emit_event(event_type: str, payload: Dict[str, Any]) -> None:
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


# ── SDK Init ────────────────────────────────────────────────────────────

def init_clob_client():
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
        log.warning("No builder creds — auto-redeem disabled")
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
    ts = REF_15M
    while ts < now_ts:
        ts += 900
    return [ts - 900, ts, ts + 900]


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
        emit_event("redeem_submitted", {"tx_id": tx_id})

        for _ in range(20):
            time.sleep(3)
            status = relayer.get_transaction(tx_id)
            if isinstance(status, list):
                status = status[0]
            state = status.get("state", "")
            if "CONFIRMED" in state:
                tx_hash = status.get("transactionHash", "")
                log.info("  Redeem CONFIRMED: %s", tx_hash[:20])
                emit_event("redeem_confirmed", {"tx_id": tx_id, "tx_hash": tx_hash})
                return True
            if "FAILED" in state or "INVALID" in state:
                msg = status.get("errorMsg", "")[:80]
                log.error("  Redeem FAILED: %s", msg)
                emit_event("redeem_failed", {
                    "tx_id": tx_id, "state": state, "error": msg,
                })
                return False
        log.warning("  Redeem timeout — check manually")
        emit_event("redeem_timeout", {"tx_id": tx_id})
        return False
    except Exception as e:
        log.error("  Redeem error: %s", e)
        emit_event("redeem_error", {"error": str(e)})
        return False


# ── Entry Gate (dynamic, retryable) ─────────────────────────────────────

def evaluate_entry(client, mkt: Market, cfg: BotConfig) -> tuple[bool, dict]:
    """Returns (should_enter, details). Soft rejections are retried next loop."""
    now = int(time.time())
    time_remaining = (mkt.ts + MARKET_DURATION) - now

    if time_remaining < cfg.min_entry_time_remaining:
        return False, {
            "reject": "hard", "reason": "too_late",
            "time_remaining": time_remaining,
        }

    if DRY_RUN:
        return True, {"reject": None, "reason": "dry_run_bypass", "combined_ask": 0, "edge": 0}

    try:
        up_book = client.get_order_book(mkt.up_token)
        dn_book = client.get_order_book(mkt.dn_token)
        up_best_ask = min(float(a.price) for a in up_book.asks) if up_book.asks else None
        dn_best_ask = min(float(a.price) for a in dn_book.asks) if dn_book.asks else None

        if up_best_ask is None or dn_best_ask is None:
            return False, {"reject": "soft", "reason": "no_book_data"}

        combined_ask = up_best_ask + dn_best_ask
        edge = 1.0 - combined_ask

        if combined_ask >= cfg.max_combined_ask:
            return False, {
                "reject": "soft", "reason": "no_edge",
                "combined_ask": round(combined_ask, 4), "edge": round(edge, 4),
            }

        up_depth = sum(float(a.size) for a in up_book.asks[:3]) if up_book.asks else 0
        dn_depth = sum(float(a.size) for a in dn_book.asks[:3]) if dn_book.asks else 0
        if up_depth < 10 or dn_depth < 10:
            return False, {
                "reject": "soft", "reason": "thin_book",
                "up_depth": round(up_depth, 1), "dn_depth": round(dn_depth, 1),
            }

        return True, {
            "combined_ask": round(combined_ask, 4), "edge": round(edge, 4),
            "up_depth": round(up_depth, 1), "dn_depth": round(dn_depth, 1),
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
            price = cfg.price
            if side == "UP":
                mkt.position.up_shares += delta
                mkt.position.up_cost += delta * price
            else:
                mkt.position.dn_shares += delta
                mkt.position.dn_cost += delta * price

            setattr(mkt, matched_attr, new_matched)
            total = mkt.position.up_shares if side == "UP" else mkt.position.dn_shares
            log.info("  FILL: %s +%.1f shares (total: %.1f)", side, delta, total)
            emit_event("fill_detected", {
                "market_ts": mkt.ts, "side": side, "delta": round(delta, 2),
                "total_matched": round(new_matched, 2), "price": price,
            })

    up_filled = mkt.position.up_shares > 0
    dn_filled = mkt.position.dn_shares > 0

    if up_filled and dn_filled and mkt.state == MState.ENTERED:
        mkt.state = MState.BOTH_FILLED
        log.info("  BOTH sides filled! hedged_profit=$%.2f", mkt.position.hedged_profit)
        emit_event("both_filled", {
            "market_ts": mkt.ts,
            "up_shares": mkt.position.up_shares,
            "dn_shares": mkt.position.dn_shares,
            "combined_vwap": round(mkt.position.combined_vwap, 4),
            "hedged_profit": round(mkt.position.hedged_profit, 2),
        })
    elif (up_filled != dn_filled) and mkt.state == MState.ENTERED:
        mkt.state = MState.ONE_SIDED
        mkt.filled_side = "UP" if up_filled else "DOWN"
        mkt.one_sided_since = time.time()
        filled_shares = mkt.position.up_shares if up_filled else mkt.position.dn_shares
        log.info("  ONE-SIDED: %s filled (%.1f), %s unfilled",
                 mkt.filled_side, filled_shares,
                 "DOWN" if up_filled else "UP")
        emit_event("one_sided_fill", {
            "market_ts": mkt.ts, "filled_side": mkt.filled_side,
            "filled_shares": round(filled_shares, 1),
        })


# ── Unwind Logic (EV-based sell/wait) ──────────────────────────────────

def evaluate_unwind(mkt: Market, ws_feed: WSBookFeed,
                    cfg: BotConfig) -> tuple[bool, dict]:
    """Decide whether to sell the filled side.

    Compares:
      - pi_exit_now: profit from selling now
      - ev_wait: EV of waiting (weighted by fill probability)
    Sell if ev_wait <= pi_exit_now + buffer, or if single-leg profit
    already meets pair target, or if hard timeout reached.
    """
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
    f = cfg.fee_per_share

    pi_exit_now = s_now - a - f
    pi_pair = 1.0 - a - b - f

    time_elapsed_frac = max(0.0, min(1.0, 1.0 - time_remaining / MARKET_DURATION))
    p_fill = max(0.05, 0.4 * (1.0 - time_elapsed_frac) ** 2)

    unfilled_token = mkt.dn_token if is_up else mkt.up_token
    opp_ask = ws_feed.get_best_ask(unfilled_token)
    if opp_ask is not None and abs(opp_ask - cfg.price) <= 0.02:
        p_fill = min(0.6, p_fill * 1.5)

    ev_wait = p_fill * pi_pair + (1.0 - p_fill) * pi_exit_now

    details = {
        "market_ts": mkt.ts, "filled_side": mkt.filled_side,
        "s_now": round(s_now, 4), "a": round(a, 4),
        "pi_exit_now": round(pi_exit_now, 4), "pi_pair": round(pi_pair, 4),
        "p_fill": round(p_fill, 4), "ev_wait": round(ev_wait, 4),
        "time_remaining": round(time_remaining, 0),
        "filled_shares": round(filled_shares, 1),
    }

    if pi_exit_now >= pi_pair:
        details["trigger"] = "profit_exceeds_pair"
        return True, details

    if ev_wait <= pi_exit_now + cfg.unwind_buffer:
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
    sell_oid = sell_at_bid(
        client, filled_token, filled_shares, side_label,
        expiration_s=cfg.order_expiry_seconds,
    )
    mkt.state = MState.UNWOUND
    emit_event("unwind_sold", {
        "market_ts": mkt.ts, "side_sold": mkt.filled_side,
        "shares": round(filled_shares, 1), "sell_order_id": sell_oid,
    })


# ── Redeem All ──────────────────────────────────────────────────────────

async def try_redeem_all(client, relayer, markets: dict, cfg: BotConfig):
    if not relayer:
        return

    for ts, mkt in list(markets.items()):
        if mkt.redeemed or mkt.state == MState.UNWOUND:
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
        emit_event("redeem_started", {
            "slug": mkt.slug, "market_ts": ts,
            "up_bal": up_bal, "dn_bal": dn_bal,
        })

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
    cfg = load_config()
    emit_event("status", {
        "message": "bot_starting", "config": asdict(cfg), "dry_run": DRY_RUN,
    })

    if DRY_RUN:
        log.info("*** DRY RUN MODE — no real orders will be placed ***\n")

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

    while True:
        now = int(time.time())
        cfg = load_config()
        timestamps = next_market_timestamps(now)

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
            emit_event("market_discovered", {
                "slug": slug, "market_ts": ts, "title": mkt.title,
            })

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
                    if up_bal > 0 and dn_bal > 0:
                        mkt.state = MState.BOTH_FILLED
                    else:
                        mkt.state = MState.ONE_SIDED
                        mkt.filled_side = "UP" if up_bal > 0 else "DOWN"
                        mkt.one_sided_since = time.time()
                    continue

                if not DRY_RUN:
                    try:
                        live_orders = client.get_orders()
                        tokens = {mkt.up_token, mkt.dn_token}
                        existing = [o for o in live_orders
                                    if o.get("status") == "LIVE"
                                    and o.get("asset_id") in tokens]
                        if existing:
                            log.info("  [%s] Found %d live order(s) -> ENTERED",
                                     mkt.slug, len(existing))
                            mkt.state = MState.ENTERED
                            mkt.entry_time = time.time()
                            for o in existing:
                                if o.get("asset_id") == mkt.up_token:
                                    mkt.up_order_id = o.get("id")
                                elif o.get("asset_id") == mkt.dn_token:
                                    mkt.dn_order_id = o.get("id")
                            continue
                    except Exception:
                        pass

                should_enter, details = evaluate_entry(client, mkt, cfg)

                if details.get("reject") == "hard":
                    log.info("  [%s] EXPIRED: %s (%ds left)",
                             mkt.slug, details.get("reason"),
                             details.get("time_remaining", 0))
                    mkt.state = MState.DONE
                    emit_event("market_expired", {
                        "slug": mkt.slug, "market_ts": ts, **details,
                    })
                    continue

                if not should_enter:
                    mkt.deferred_count += 1
                    if mkt.deferred_count <= 3 or mkt.deferred_count % 6 == 0:
                        log.info("  [%s] DEFERRED (%dx): %s (ask=%.3f)",
                                 mkt.slug, mkt.deferred_count,
                                 details.get("reason", "?"),
                                 details.get("combined_ask", 0))
                    emit_event("market_deferred", {
                        "slug": mkt.slug, "market_ts": ts, **details,
                    })
                    continue

                log.info("  [%s] ENTERING (edge=%.3f)", mkt.slug,
                         details.get("edge", 0))
                emit_event("edge_check", {
                    "slug": mkt.slug, "market_ts": ts,
                    "combined_ask": details.get("combined_ask", 0),
                    "edge": details.get("edge", 0),
                })

                up_id = place_order(
                    client, mkt.up_token, "UP  ",
                    cfg.shares_per_side, cfg.price, cfg.order_expiry_seconds,
                )
                if up_id:
                    mkt.up_order_id = up_id
                    emit_event("order_placed", {
                        "slug": mkt.slug, "market_ts": ts, "side": "UP",
                        "shares": cfg.shares_per_side, "price": cfg.price,
                        "order_id": up_id,
                    })

                dn_id = place_order(
                    client, mkt.dn_token, "DOWN",
                    cfg.shares_per_side, cfg.price, cfg.order_expiry_seconds,
                )
                if dn_id:
                    mkt.dn_order_id = dn_id
                    emit_event("order_placed", {
                        "slug": mkt.slug, "market_ts": ts, "side": "DOWN",
                        "shares": cfg.shares_per_side, "price": cfg.price,
                        "order_id": dn_id,
                    })

                placed = (1 if up_id else 0) + (1 if dn_id else 0)
                log.info("  %d orders placed. Max cost: $%.2f",
                         placed, cfg.shares_per_side * cfg.price * 2)

                mkt.state = MState.ENTERED
                mkt.entry_time = time.time()
                emit_event("market_entered", {
                    "slug": mkt.slug, "market_ts": ts, "title": mkt.title,
                })

            # ── ENTERED: monitor fills ──
            elif mkt.state == MState.ENTERED:
                check_fills(client, mkt, cfg)

                time_remaining = (mkt.ts + MARKET_DURATION) - now
                no_fills = (mkt.position.up_shares == 0
                            and mkt.position.dn_shares == 0)
                if time_remaining < 0 and no_fills:
                    log.info("  [%s] Market ended with no fills", mkt.slug)
                    mkt.state = MState.DONE
                    emit_event("market_no_fills", {
                        "slug": mkt.slug, "market_ts": ts,
                    })

            # ── ONE_SIDED: unwind evaluation ──
            elif mkt.state == MState.ONE_SIDED:
                check_fills(client, mkt, cfg)
                if mkt.state == MState.BOTH_FILLED:
                    continue

                should_sell, details = evaluate_unwind(mkt, ws_feed, cfg)
                emit_event("unwind_check", details)

                if should_sell:
                    log.info(
                        "  [%s] UNWIND: %s | exit=%.4f ev_wait=%.4f p=%.3f",
                        mkt.slug, details.get("trigger"),
                        details.get("pi_exit_now", 0),
                        details.get("ev_wait", 0),
                        details.get("p_fill", 0),
                    )
                    execute_unwind(client, mkt, cfg)
                else:
                    log.info(
                        "  [%s] WAIT: ev=%.4f > exit=%.4f (p=%.3f, %ds left)",
                        mkt.slug,
                        details.get("ev_wait", 0),
                        details.get("pi_exit_now", 0),
                        details.get("p_fill", 0),
                        details.get("time_remaining", 0),
                    )

            # ── BOTH_FILLED / UNWOUND / DONE: handled by redeem or no-op ──

        # Phase 3: emit WS price updates for active markets
        for ts, mkt in markets.items():
            if mkt.state in (MState.DONE, MState.UNWOUND):
                continue
            up_ask = ws_feed.get_best_ask(mkt.up_token)
            dn_ask = ws_feed.get_best_ask(mkt.dn_token)
            if up_ask is not None or dn_ask is not None:
                emit_event("price_update", {
                    "market_ts": ts,
                    "up_ask": up_ask or 0,
                    "dn_ask": dn_ask or 0,
                })

        # Phase 4: redeem resolved markets
        await try_redeem_all(client, relayer, markets, cfg)

        # Phase 5: cleanup old markets (keep 2h for redemption)
        markets = {ts: m for ts, m in markets.items() if ts > now - 7200}

        await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("\nStopped.")
