"""
Place limit orders on both UP and DOWN for BTC 15-min markets.
State-machine-based: tracks paired vs unpaired shares, handles one-sided fills
via repair (FAK buy) or unwind (FAK sell), redeems resolved positions.

Usage: python scripts/place_45.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.bot.ws_book_feed import WSBookFeed
from src.config import calculate_taker_fee

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("place45")


# =========================================================
# STATE ENUMS
# =========================================================

class MarketState(Enum):
    WATCHING = "WATCHING"
    ENTRY_ORDERS_WORKING = "ENTRY_ORDERS_WORKING"
    ONE_SIDED_FILL = "ONE_SIDED_FILL"
    REPAIRING = "REPAIRING"
    BOTH_FILLED = "BOTH_FILLED"
    UNWINDING = "UNWINDING"
    WAIT_RESOLUTION = "WAIT_RESOLUTION"
    REDEEMING = "REDEEMING"
    DONE = "DONE"
    ERROR = "ERROR"


# =========================================================
# CONFIG
# =========================================================

@dataclass
class BotConfig:
    asset: str = "btc"
    price: float = 0.45
    entry_price: float = 0.45
    shares_per_side: float = 60
    max_combined_ask: float = 0.98
    bail_price: float = 0.72
    bail_enabled: bool = True
    order_expiry_seconds: int = 300
    entry_expiry_seconds: int = 300
    rebalance_expiry_seconds: int = 60
    exit_expiry_seconds: int = 15
    max_one_sided_seconds: int = 20
    recheck_interval_seconds: int = 2
    market_lookahead_count: int = 3
    max_recent_history_seconds: int = 7200
    enable_passive_entry: bool = True
    enable_aggressive_repair: bool = True
    enable_redeem: bool = True
    dry_run: bool = False
    entry_cutoff_seconds_before_close: int = 60
    min_repair_shares: float = 1.0
    repair_trigger_shares: float = 1.0
    must_flatten_shares_before_close: float = 0.1
    min_repair_edge_per_share: float = 0.0
    max_repair_attempts: int = 2
    max_unwind_attempts: int = 2


CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.jsonl")
STATE_PATH = os.path.join(PROJECT_ROOT, "place45_state.json")

# In-memory metadata for orders we place (order_id -> {purpose, tif, created_ts, slug})
_placed_order_meta: Dict[str, dict] = {}
# Track size_matched per order to build fill lots (order_id -> last size_matched)
_order_fills_tracked: Dict[str, float] = {}
# Map order_id -> slug for disappeared order lookup
_order_to_slug: Dict[str, str] = {}
# Retry count for disappeared orders (oid -> retries)
_missing_order_retries: Dict[str, int] = {}


def load_config() -> BotConfig:
    """Load config from config.json, falling back to defaults."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = BotConfig()
    for f in asdict(cfg).keys():
        if f in data:
            setattr(cfg, f, data[f])
    # Backward compat: map legacy names
    if "price" in data and "entry_price" not in data:
        cfg.entry_price = data["price"]
    if "order_expiry_seconds" in data and "entry_expiry_seconds" not in data:
        cfg.entry_expiry_seconds = data["order_expiry_seconds"]
    return cfg


# =========================================================
# ORDER / MARKET TRACKING
# =========================================================

@dataclass
class FillLot:
    """Current inventory lot after applying buy/sell fills.

    Price is from order limit; execution price may differ for taker/partial fills.
    """
    side: str
    shares: float
    price: float
    fee_paid: float = 0.0
    is_estimated: bool = True  # True when using order limit price; API may not expose execution price


@dataclass
class OrderRef:
    order_id: str
    side: str
    action: str
    price: float
    shares: float
    status: str
    purpose: str = "ENTRY"
    tif: str = "GTD"
    created_ts: int = 0


@dataclass
class MarketRuntime:
    ts: int
    slug: str
    title: str
    up_token: str
    dn_token: str
    condition_id: str
    neg_risk: bool

    state: MarketState = MarketState.WATCHING
    last_transition_ts: int = 0
    last_action_ts: int = 0

    up_orders: List[OrderRef] = field(default_factory=list)
    dn_orders: List[OrderRef] = field(default_factory=list)

    up_fill_lots: List[FillLot] = field(default_factory=list)
    dn_fill_lots: List[FillLot] = field(default_factory=list)

    up_filled_shares: float = 0.0
    dn_filled_shares: float = 0.0
    paired_shares: float = 0.0
    unpaired_up: float = 0.0
    unpaired_dn: float = 0.0

    up_live_buy_shares: float = 0.0
    dn_live_buy_shares: float = 0.0
    up_live_sell_shares: float = 0.0
    dn_live_sell_shares: float = 0.0

    up_total_cost: float = 0.0
    dn_total_cost: float = 0.0
    locked_cost: float = 0.0
    locked_value: float = 0.0
    locked_edge: float = 0.0

    closed: bool = False
    close_ts: int = 0
    redeemed: bool = False
    errored: bool = False
    repair_attempts: int = 0
    unwind_attempts: int = 0
    both_filled_cleanup_done: bool = False


def emit_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Append a single JSON event line to events.jsonl. Best-effort only."""
    event = {
        "type": event_type,
        "ts": int(time.time()),
        "payload": payload,
    }
    try:
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        # Logging is already going to journal; avoid noisy errors here.
        pass


# =========================================================
# PERSISTED STATE (survives restart)
# =========================================================

def _fill_lot_to_dict(lot: FillLot) -> dict:
    return {
        "side": lot.side,
        "shares": lot.shares,
        "price": lot.price,
        "fee_paid": lot.fee_paid,
        "is_estimated": lot.is_estimated,
    }


def _dict_to_fill_lot(d: dict) -> FillLot:
    return FillLot(
        side=d.get("side", ""),
        shares=float(d.get("shares", 0)),
        price=float(d.get("price", 0)),
        fee_paid=float(d.get("fee_paid", 0)),
        is_estimated=d.get("is_estimated", True),
    )


def load_persisted_state() -> Dict[str, Dict[str, list]]:
    """Load _placed_order_meta, _order_fills_tracked, _order_to_slug from disk. Returns fill_lots by slug."""
    global _placed_order_meta, _order_fills_tracked, _order_to_slug
    fill_lots: Dict[str, Dict[str, list]] = {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _placed_order_meta = data.get("placed_order_meta", {})
        _order_fills_tracked = {k: float(v) for k, v in data.get("order_fills_tracked", {}).items()}
        _order_to_slug = data.get("order_to_slug", {})
        raw = data.get("fill_lots", {})
        for slug, lots in raw.items():
            fill_lots[slug] = {
                "up": [_dict_to_fill_lot(d) for d in lots.get("up", [])],
                "dn": [_dict_to_fill_lot(d) for d in lots.get("dn", [])],
            }
        log.info("Loaded persisted state: %d order meta, %d fills tracked", len(_placed_order_meta), len(_order_fills_tracked))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Failed to load state file: %s", e)
    return fill_lots


def _build_fill_lots_from_markets(tracked_markets: Dict[int, MarketRuntime]) -> Dict[str, Dict[str, list]]:
    """Build fill_lots dict from tracked markets for persistence."""
    out: Dict[str, Dict[str, list]] = {}
    for m in tracked_markets.values():
        if m.up_fill_lots or m.dn_fill_lots:
            out[m.slug] = {
                "up": m.up_fill_lots,
                "dn": m.dn_fill_lots,
            }
    return out


def save_persisted_state(fill_lots_by_slug: Optional[Dict[str, Dict[str, list]]] = None) -> None:
    """Persist state to disk."""
    fill_lots = fill_lots_by_slug or {}
    data = {
        "placed_order_meta": _placed_order_meta,
        "order_fills_tracked": _order_fills_tracked,
        "order_to_slug": _order_to_slug,
        "fill_lots": {
            slug: {
                "up": [_fill_lot_to_dict(l) for l in lots.get("up", [])],
                "dn": [_fill_lot_to_dict(l) for l in lots.get("dn", [])],
            }
            for slug, lots in fill_lots.items()
        },
    }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
    except Exception as e:
        log.warning("Failed to save state: %s", e)


def replay_fill_events_from_journal(tracked_markets: Dict[int, MarketRuntime]) -> None:
    """Rebuild order metadata from events.jsonl (order_placed). Fill lots come from state file."""
    try:
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return

    for line in lines:
        try:
            event = json.loads(line.strip())
            typ = event.get("type", "")
            payload = event.get("payload", {})
            ts = event.get("ts", 0)

            if typ == "order_placed":
                oid = payload.get("order_id", "")
                slug = payload.get("slug", "")
                if oid and slug:
                    _placed_order_meta[oid] = {
                        "purpose": payload.get("purpose", "ENTRY"),
                        "tif": payload.get("tif", "GTD"),
                        "created_ts": ts,
                        "slug": slug,
                    }
                    _order_to_slug[oid] = slug
        except Exception:
            continue


# --- Constants (configurable via BotConfig) ---
GAMMA_URL = "https://gamma-api.polymarket.com"
REF_15M = 1771268400     # known 15m epoch anchor
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


# =========================================================
# ASYNC WRAPPERS FOR SYNC SDK
# =========================================================

async def _run_sdk(fn, *args, **kwargs):
    """Run sync CLOB SDK call in thread pool to avoid blocking event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _price_from_level(level) -> float:
    """Extract price from order book level (object or dict)."""
    if hasattr(level, "price"):
        return float(level.price)
    if isinstance(level, dict):
        return float(level.get("price", 0))
    return 0.0


async def get_best_ask_sync(client, token_id: str) -> Optional[float]:
    """Get best ask from REST order book."""
    try:
        book = await _run_sdk(client.get_order_book, token_id)
        if not book or not getattr(book, "asks", None):
            return None
        asks = book.asks
        if not asks:
            return None
        return min(_price_from_level(a) for a in asks)
    except Exception:
        return None


async def get_best_bid_sync(client, token_id: str) -> Optional[float]:
    """Get best bid from REST order book."""
    try:
        book = await _run_sdk(client.get_order_book, token_id)
        if not book or not getattr(book, "bids", None):
            return None
        bids = book.bids
        if not bids:
            return None
        return max(_price_from_level(b) for b in bids)
    except Exception:
        return None


def next_market_timestamps(now_ts: int) -> list[int]:
    """Return [currently_trading, next, after_that] 15m market timestamps.

    The slug timestamp is the START time of the market.
    """
    ts = REF_15M
    while ts < now_ts:
        ts += 900
    # ts = next market to start. ts-900 = currently trading market.
    return [ts - 900, ts, ts + 900]


def scan_recent_market_timestamps(max_seconds: int) -> list[int]:
    """Return 15m-aligned timestamps from (now - max_seconds) to now."""
    now = int(time.time())
    start = now - max_seconds
    ts = REF_15M
    while ts < start:
        ts += 900
    result = []
    while ts <= now:
        result.append(ts)
        ts += 900
    return result


def _parse_json_field(value: str | list) -> list:
    """Parse a field that might be a JSON string or already a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


async def get_market_info(slug: str) -> dict:
    """Fetch market info from gamma API."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{GAMMA_URL}/events", params={"slug": slug})
        r.raise_for_status()
        data = r.json()
    if not data:
        raise ValueError(f"No event found for slug: {slug}")
    m = data[0]["markets"][0]
    tokens = _parse_json_field(m.get("clobTokenIds", []))
    outcomes = _parse_json_field(m.get("outcomes", []))

    # Map tokens by outcome label — do not assume array order
    up_token = None
    dn_token = None
    for i, outcome in enumerate(outcomes):
        name = outcome.lower() if isinstance(outcome, str) else ""
        tok = tokens[i] if i < len(tokens) else None
        if name in ("up", "yes"):
            up_token = tok
        elif name in ("down", "no"):
            dn_token = tok

    if not up_token or not dn_token:
        raise ValueError(f"Could not map UP/DOWN tokens from outcomes: {outcomes}")

    close_ts = 0
    end_iso = m.get("endDate", "")
    if end_iso:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            close_ts = int(dt.timestamp())
        except Exception:
            pass

    return {
        "up_token": up_token,
        "dn_token": dn_token,
        "title": m.get("question", slug),
        "conditionId": m.get("conditionId", ""),
        "closed": m.get("closed", False),
        "neg_risk": m.get("negRisk", False),
        "close_ts": close_ts,
    }


async def fetch_market_runtime(ts: int, cfg: BotConfig) -> MarketRuntime:
    """Fetch market info and build MarketRuntime."""
    slug = f"{cfg.asset}-updown-15m-{ts}"
    info = await get_market_info(slug)
    now_ts = int(time.time())
    return MarketRuntime(
        ts=ts,
        slug=slug,
        title=info["title"],
        up_token=info["up_token"],
        dn_token=info["dn_token"],
        condition_id=info["conditionId"],
        neg_risk=info.get("neg_risk", False),
        close_ts=info.get("close_ts", 0),
        last_transition_ts=now_ts,
        last_action_ts=0,
    )


def init_clob_client():
    """Initialize py-clob-client SDK."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.getenv("POLYMARKET_FUNDER", "")
    kwargs = {"funder": funder} if funder else {}

    client = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        signature_type=1,
        **kwargs,
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
    """Initialize builder relayer client for gasless redeems."""
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


# =========================================================
# ORDER HELPERS
# =========================================================

async def place_buy_limit(
    client, token_id: str, shares: float, price: float,
    expiry_s: int, label: str, dry_run: bool = False,
    purpose: str = "ENTRY", slug: str = "", post_only: bool = True,
) -> Optional[str]:
    """Place GTD limit buy. Returns order ID or None."""
    if dry_run:
        log.info("  [DRY] %s BUY %.0f @ %.2f", label, shares, price)
        return f"dry-{label}-{int(time.time())}"
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType

    expiration = int(time.time()) + int(expiry_s)
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=round(shares, 1),
        side=BUY,
        expiration=expiration,
    )
    try:
        signed = await _run_sdk(client.create_order, order_args)
        result = await _run_sdk(client.post_order, signed, OrderType.GTD, post_only)
        oid = result.get("orderID", result.get("id", ""))
        if oid:
            created_ts = int(time.time())
            tif = "GTD_POST_ONLY" if post_only else "GTD"
            _placed_order_meta[oid] = {"purpose": purpose, "tif": tif, "created_ts": created_ts, "slug": slug}
            _order_to_slug[oid] = slug
            emit_event("order_placed", {"order_id": oid, "slug": slug, "side": "DOWN" if "DOWN" in label.upper() else "UP", "purpose": purpose, "tif": tif, "created_ts": created_ts})
            log.info("  %s BUY %.0f @ %.2f  [%s]", label, shares, price, oid[:12])
            return oid
        else:
            log.warning("  %s no order ID: %s", label, result)
    except Exception as e:
        log.error("  %s order failed: %s", label, e)
    return None


async def place_buy_fak(
    client, token_id: str, shares: float, price: float,
    label: str, dry_run: bool = False, purpose: str = "REPAIR", slug: str = "",
) -> Optional[str]:
    """Place FAK buy at given price (aggressive repair)."""
    if dry_run:
        log.info("  [DRY] %s FAK BUY %.0f @ %.2f", label, shares, price)
        return f"dry-fak-{label}-{int(time.time())}"
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType

    expiration = int(time.time()) + 60
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=round(shares, 1),
        side=BUY,
        expiration=expiration,
    )
    try:
        signed = await _run_sdk(client.create_order, order_args)
        result = await _run_sdk(client.post_order, signed, OrderType.FAK)
        oid = result.get("orderID", result.get("id", ""))
        if oid:
            created_ts = int(time.time())
            _placed_order_meta[oid] = {"purpose": purpose, "tif": "FAK", "created_ts": created_ts, "slug": slug}
            _order_to_slug[oid] = slug
            emit_event("order_placed", {"order_id": oid, "slug": slug, "side": "DOWN" if "DOWN" in label.upper() else "UP", "purpose": purpose, "tif": "FAK", "created_ts": created_ts})
            log.info("  %s FAK BUY %.0f @ %.2f  [%s]", label, shares, price, oid[:12])
            return oid
    except Exception as e:
        log.error("  %s FAK BUY failed: %s", label, e)
    return None


async def place_sell_fak(
    client, token_id: str, shares: float, price: float,
    label: str, dry_run: bool = False, purpose: str = "UNWIND", slug: str = "",
) -> Optional[str]:
    """Place FAK sell at given price (unwind)."""
    if dry_run:
        log.info("  [DRY] %s FAK SELL %.0f @ %.2f", label, shares, price)
        return f"dry-fak-sell-{label}-{int(time.time())}"
    from py_clob_client.order_builder.constants import SELL
    from py_clob_client.clob_types import OrderArgs, OrderType

    expiration = int(time.time()) + 60
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=round(shares, 1),
        side=SELL,
        expiration=expiration,
    )
    try:
        signed = await _run_sdk(client.create_order, order_args)
        result = await _run_sdk(client.post_order, signed, OrderType.FAK)
        oid = result.get("orderID", result.get("id", ""))
        if oid:
            created_ts = int(time.time())
            _placed_order_meta[oid] = {"purpose": purpose, "tif": "FAK", "created_ts": created_ts, "slug": slug}
            _order_to_slug[oid] = slug
            emit_event("order_placed", {"order_id": oid, "slug": slug, "side": "DOWN" if "DOWN" in label.upper() else "UP", "purpose": purpose, "tif": "FAK", "created_ts": created_ts})
            log.info("  %s FAK SELL %.0f @ %.2f  [%s]", label, shares, price, oid[:12])
            return oid
    except Exception as e:
        log.error("  %s FAK SELL failed: %s", label, e)
    return None


async def cancel_market_orders_async(client, token_ids: set, dry_run: bool = False) -> None:
    """Cancel all live orders for the given token IDs."""
    if dry_run:
        log.info("  [DRY] Would cancel orders for tokens")
        return
    try:
        orders = await _run_sdk(client.get_orders, None)
        orders = orders if orders else []
        to_cancel = [o["id"] for o in orders
                     if o.get("status") == "LIVE"
                     and o.get("asset_id") in token_ids
                     and "id" in o]
        if to_cancel:
            await _run_sdk(client.cancel_orders, to_cancel)
            log.info("  Cancelled %d order(s)", len(to_cancel))
    except Exception as e:
        log.error("  Cancel failed: %s", e)


async def cancel_stale_entry_orders(client, m: MarketRuntime, dry_run: bool = False) -> None:
    """Cancel live BUY orders with purpose ENTRY only."""
    order_ids = []
    for o in m.up_orders + m.dn_orders:
        if o.action == "BUY" and o.status == "LIVE" and getattr(o, "purpose", "ENTRY") == "ENTRY":
            order_ids.append(o.order_id)
    if not order_ids:
        return
    if dry_run:
        log.info("  [DRY] Would cancel %d entry order(s)", len(order_ids))
        return
    try:
        await _run_sdk(client.cancel_orders, order_ids)
        log.info("  Cancelled %d stale entry order(s)", len(order_ids))
        emit_event("cancel_stale_entry_orders", {"slug": m.slug, "count": len(order_ids)})
    except Exception as e:
        log.error("  Cancel stale failed: %s", e)


async def redeem_market(relayer, condition_id: str, neg_risk: bool) -> bool:
    """Redeem resolved positions via gasless relayer. Returns True on success."""
    from web3 import Web3
    from eth_abi import encode
    from py_builder_relayer_client.models import SafeTransaction, OperationType

    if neg_risk:
        # NegRiskAdapter.redeemPositions(bytes32, uint256[])
        # We don't know exact amounts, but we can pass max uint for both
        # Actually for neg-risk we need the adapter address and different encoding
        NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
        cond_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
        # Pass [max, max] — contract will only burn what's available
        max_uint = 2**256 - 1
        selector = Web3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
        params = encode(["bytes32", "uint256[]"], [cond_bytes, [max_uint, max_uint]])
        target = NEG_RISK_ADAPTER
    else:
        # CTF.redeemPositions(address, bytes32, bytes32, uint256[])
        cond_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
        selector = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        params = encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_ADDRESS, b"\x00" * 32, cond_bytes, [1, 2]],
        )
        target = CTF_ADDRESS

    calldata = "0x" + (selector + params).hex()
    tx = SafeTransaction(to=target, operation=OperationType.Call, data=calldata, value="0")

    try:
        resp = await asyncio.to_thread(relayer.execute, [tx], "Redeem positions")
        tx_id = getattr(resp, "transaction_id", None) or getattr(resp, "id", None)
        log.info("  Redeem submitted: %s", tx_id)
        emit_event("redeem_submitted", {"tx_id": tx_id})

        # Poll for result (async-safe to avoid blocking event loop)
        for _ in range(20):
            await asyncio.sleep(3)
            status = await asyncio.to_thread(relayer.get_transaction, tx_id)
            if isinstance(status, list):
                status = status[0]
            state = status.get("state", "")
            if "CONFIRMED" in state:
                tx_hash = status.get("transactionHash", "")
                log.info("  Redeem CONFIRMED: %s", tx_hash[:20])
                emit_event(
                    "redeem_confirmed",
                    {
                        "tx_id": tx_id,
                        "tx_hash": tx_hash,
                    },
                )
                return True
            if "FAILED" in state or "INVALID" in state:
                msg = status.get("errorMsg", "")[:80]
                log.error("  Redeem FAILED: %s", msg)
                emit_event(
                    "redeem_failed",
                    {
                        "tx_id": tx_id,
                        "state": state,
                        "error": msg,
                    },
                )
                return False
        log.warning("  Redeem timeout — check manually")
        emit_event("redeem_timeout", {"tx_id": tx_id})
        return False
    except Exception as e:
        log.error("  Redeem error: %s", e)
        emit_event("redeem_error", {"error": str(e)})
        return False


async def check_token_balance_async(client, token_id: str) -> float:
    """Check CTF token balance via CLOB API (returns shares)."""
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    try:
        bal = await _run_sdk(
            client.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2)
        )
        return int(bal.get("balance", "0")) / 1e6
    except Exception:
        return 0.0


async def check_usdc_balance_async(client) -> float:
    """Check available USDC balance via CLOB API."""
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    try:
        bal = await _run_sdk(
            client.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return int(bal.get("balance", "0")) / 1e6
    except Exception:
        return -1.0


# =========================================================
# HELPERS
# =========================================================

def cost_of_first_n_shares(fill_lots: List[FillLot], n: float) -> float:
    """FIFO cost of first n shares from fill lots.
    Fill lots use order limit price; API may not expose execution price.
    locked_cost is APPROXIMATE, not exact, especially for taker/FAK flows.
    """
    remaining = n
    total = 0.0
    for lot in fill_lots:
        if remaining <= 0:
            break
        take = min(remaining, lot.shares)
        per_share = lot.price + (lot.fee_paid / lot.shares if lot.shares > 0 else 0.0)
        total += take * per_share
        remaining -= take
    return total


def cost_of_last_n_shares(fill_lots: List[FillLot], n: float) -> float:
    """FIFO inventory model: excess shares are the newest remaining lots."""
    remaining = n
    total = 0.0
    for lot in reversed(fill_lots):
        if remaining <= 0:
            break
        take = min(remaining, lot.shares)
        per_share = lot.price + (lot.fee_paid / lot.shares if lot.shares > 0 else 0.0)
        total += take * per_share
        remaining -= take
    return total


def inventory_shares(fill_lots: List[FillLot]) -> float:
    return sum(max(0.0, lot.shares) for lot in fill_lots)


def inventory_cost(fill_lots: List[FillLot]) -> float:
    return sum((lot.shares * lot.price) + lot.fee_paid for lot in fill_lots if lot.shares > 0)


def add_inventory_fill(fill_lots: List[FillLot], side: str, shares: float, price: float, fee_paid: float) -> None:
    if shares <= 0:
        return
    fill_lots.append(
        FillLot(
            side=side,
            shares=shares,
            price=price,
            fee_paid=fee_paid,
            is_estimated=True,
        )
    )


def consume_inventory_lots(fill_lots: List[FillLot], shares: float) -> float:
    """Consume current inventory FIFO and return consumed cost basis."""
    remaining = shares
    consumed_cost = 0.0
    while remaining > 0 and fill_lots:
        lot = fill_lots[0]
        take = min(remaining, lot.shares)
        if take > 0:
            per_share = lot.price + (lot.fee_paid / lot.shares if lot.shares > 0 else 0.0)
            consumed_cost += take * per_share
            lot.shares -= take
            lot.fee_paid = max(0.0, lot.fee_paid - (per_share - lot.price) * take)
            remaining -= take
        if lot.shares <= 0.000001:
            fill_lots.pop(0)
    return consumed_cost


def rebalance_inventory_to_balance(
    fill_lots: List[FillLot],
    target_shares: float,
    fallback_price: float,
    side: str,
    slug: str,
) -> None:
    """Keep inventory lots aligned with exchange balances after restart/missed status gaps."""
    current = inventory_shares(fill_lots)
    diff = target_shares - current
    if abs(diff) <= 0.01:
        return
    if diff > 0:
        add_inventory_fill(fill_lots, side, diff, fallback_price, 0.0)
        emit_event(
            "inventory_reconciled_add",
            {"slug": slug, "side": side, "shares": diff, "price": fallback_price},
        )
        return
    removed_cost = consume_inventory_lots(fill_lots, -diff)
    emit_event(
        "inventory_reconciled_trim",
        {"slug": slug, "side": side, "shares": -diff, "cost_removed": removed_cost},
    )


def estimate_taker_fee(shares: float, price: float) -> float:
    """Estimate taker fee conservatively for Polymarket crypto markets."""
    try:
        return max(0.0, float(calculate_taker_fee(shares, price)))
    except Exception:
        p = max(0.0, min(1.0, float(price)))
        return shares * 0.02 * p * (1.0 - p) * 4.0


def normalize_action(side_raw) -> str:
    """Map API side to BUY or SELL. Raises if unknown."""
    s = str(side_raw).upper() if side_raw is not None else ""
    if s in ("BUY", "0", "BID"):
        return "BUY"
    if s in ("SELL", "1", "ASK"):
        return "SELL"
    raise ValueError(f"Unknown order side: {side_raw!r}")


def normalize_status(status_raw) -> str:
    """Map API status to canonical string."""
    s = str(status_raw).upper() if status_raw is not None else ""
    if s in ("LIVE", "OPEN", "ACTIVE", "PENDING"):
        return "LIVE"
    if s in ("MATCHED", "FILLED", "EXECUTED"):
        return "FILLED"
    if s in ("CANCELLED", "CANCELED"):
        return "CANCELLED"
    if s in ("EXPIRED", "REJECTED", "FAILED", "INVALID"):
        return s
    return s or "LIVE"


def _live_buy_size(orders: List[OrderRef]) -> float:
    """Sum shares of live BUY orders."""
    return sum(o.shares for o in orders if o.action == "BUY" and o.status == "LIVE")


def _live_sell_size(orders: List[OrderRef]) -> float:
    """Sum shares of live SELL orders."""
    return sum(o.shares for o in orders if o.action == "SELL" and o.status == "LIVE")


def _remaining_order_size(order: dict) -> float:
    original = float(order.get("original_size", order.get("size", 0)) or 0)
    matched = float(order.get("size_matched", 0) or 0)
    return max(0.0, original - matched)


def get_market_phase(m: MarketRuntime, now_ts: int, cutoff_secs: int) -> str:
    """Derive phase: PRE_OPEN, ACTIVE, NEAR_EXPIRY, CLOSED."""
    if m.closed:
        return "CLOSED"
    if m.close_ts > 0 and (m.close_ts - now_ts) < cutoff_secs:
        return "NEAR_EXPIRY"
    if now_ts < m.ts:
        return "PRE_OPEN"
    return "ACTIVE"


# =========================================================
# RECONCILIATION
# =========================================================

def _normalize_orders(orders: list, up_token: str, dn_token: str) -> tuple[List[OrderRef], List[OrderRef]]:
    """Split orders into up_orders and dn_orders as OrderRef list."""
    up_orders = []
    dn_orders = []
    for o in orders or []:
        oid = o.get("id", o.get("order_id", ""))
        if not oid:
            continue
        asset = o.get("asset_id", "")
        side = "UP" if asset == up_token else "DOWN"
        try:
            action = normalize_action(o.get("side"))
        except ValueError:
            log.warning("Unknown order side %r for order %s, skipping", o.get("side"), oid)
            continue
        status = normalize_status(o.get("status"))
        price = float(o.get("price", 0))
        size = _remaining_order_size(o) if status == "LIVE" else float(o.get("original_size", o.get("size", 0)))
        meta = _placed_order_meta.get(oid, {})
        purpose = meta.get("purpose", "ENTRY")
        tif = meta.get("tif", "GTD")
        created_ts = meta.get("created_ts", 0)
        ref = OrderRef(
            order_id=oid, side=side, action=action, price=price, shares=size, status=status,
            purpose=purpose, tif=tif, created_ts=created_ts,
        )
        if asset == up_token:
            up_orders.append(ref)
        elif asset == dn_token:
            dn_orders.append(ref)
    return up_orders, dn_orders


def _apply_matched_delta(
    m: MarketRuntime,
    oid: str,
    side: str,
    action: str,
    tif: str,
    delta: float,
    price: float,
) -> None:
    if delta <= 0.001:
        return
    fee_paid = estimate_taker_fee(delta, price) if tif in ("FAK", "FOK", "GTC") else 0.0
    lots = m.up_fill_lots if side == "UP" else m.dn_fill_lots
    if action == "BUY":
        add_inventory_fill(lots, side, delta, price, fee_paid)
        emit_event(
            "fill_delta",
            {"order_id": oid, "slug": m.slug, "side": side, "action": action, "delta": delta, "price": price, "fee_paid": fee_paid},
        )
        return

    consumed_cost = consume_inventory_lots(lots, delta)
    proceeds = (delta * price) - fee_paid
    emit_event(
        "fill_delta",
        {
            "order_id": oid,
            "slug": m.slug,
            "side": side,
            "action": action,
            "delta": delta,
            "price": price,
            "fee_paid": fee_paid,
            "consumed_cost": consumed_cost,
            "realized_pnl": proceeds - consumed_cost,
        },
    )


async def refresh_order_statuses(client, m: MarketRuntime) -> None:
    """Update fill lots from order size_matched. Reconcile orders + fetch missing."""
    market_tokens = {m.up_token, m.dn_token}
    seen_oids = set()

    try:
        from py_clob_client.clob_types import OpenOrderParams
        orders = await _run_sdk(client.get_orders, OpenOrderParams(market=m.condition_id))
        if not orders:
            orders = await _run_sdk(client.get_orders, None)
        orders = orders or []
        orders = [o for o in orders if o.get("asset_id") in market_tokens]
    except Exception:
        orders = []

    for o in orders:
        oid = o.get("id", o.get("order_id", ""))
        if not oid:
            continue
        seen_oids.add(oid)
        _order_to_slug[oid] = m.slug
        try:
            action = normalize_action(o.get("side"))
        except ValueError:
            continue
        size_matched = float(o.get("size_matched", 0))
        price = float(o.get("price", 0))
        asset = o.get("asset_id", "")
        side = "UP" if asset == m.up_token else "DOWN"
        meta = _placed_order_meta.get(oid, {})
        tif = meta.get("tif", "GTD")
        prev = _order_fills_tracked.get(oid, 0)
        delta = size_matched - prev
        if delta > 0.001:
            _apply_matched_delta(m, oid, side, action, tif, delta, price)
            _order_fills_tracked[oid] = size_matched
        elif size_matched > 0:
            _order_fills_tracked[oid] = size_matched

    # For orders we placed that disappeared, fetch final status (with retry)
    for oid in list(_order_fills_tracked.keys()):
        if oid in seen_oids:
            continue
        if _order_to_slug.get(oid) != m.slug:
            continue
        try:
            detail = await _run_sdk(client.get_order, oid)
            if detail:
                try:
                    action = normalize_action(detail.get("side"))
                except ValueError:
                    log.warning("Unknown order side %r for disappeared order %s, skipping final fetch", detail.get("side"), oid)
                    continue
                size_matched = float(detail.get("size_matched", 0))
                price = float(detail.get("price", 0))
                asset = detail.get("asset_id", "")
                side = "UP" if asset == m.up_token else "DOWN"
                meta = _placed_order_meta.get(oid, {})
                tif = meta.get("tif", "GTD")
                prev = _order_fills_tracked.get(oid, 0)
                delta = size_matched - prev
                if delta > 0.001:
                    _apply_matched_delta(m, oid, side, action, tif, delta, price)
                    _order_fills_tracked[oid] = size_matched
                else:
                    _order_fills_tracked[oid] = size_matched
                _missing_order_retries.pop(oid, None)
                continue
        except Exception:
            pass
        retries = _missing_order_retries.get(oid, 0) + 1
        _missing_order_retries[oid] = retries
        emit_event("order_disappeared_retry", {"order_id": oid, "slug": m.slug, "retry": retries})
        if retries >= 3:
            emit_event("order_disappeared_final", {"order_id": oid, "slug": m.slug})
            del _order_fills_tracked[oid]
            del _missing_order_retries[oid]


async def reconcile_market_state(client, ws_feed: WSBookFeed, m: MarketRuntime, cfg: BotConfig) -> None:
    """Rebuild truth from exchange: orders, balances, paired/unpaired, locked economics."""
    # 1) Refresh market closed state and close_ts
    try:
        info = await get_market_info(m.slug)
        m.closed = info.get("closed", False)
        m.close_ts = info.get("close_ts", m.close_ts)
    except Exception:
        pass

    # 2) Refresh orders and fill lots
    try:
        from py_clob_client.clob_types import OpenOrderParams
        orders = await _run_sdk(client.get_orders, OpenOrderParams(market=m.condition_id))
        if not orders:
            orders = await _run_sdk(client.get_orders, None)
        orders = orders or []
        market_tokens = {m.up_token, m.dn_token}
        orders = [o for o in orders if o.get("asset_id") in market_tokens]
        m.up_orders, m.dn_orders = _normalize_orders(orders, m.up_token, m.dn_token)
        await refresh_order_statuses(client, m)
    except Exception as e:
        log.debug("  reconcile orders failed for %s: %s", m.slug[:30], e)
        m.up_orders = []
        m.dn_orders = []

    # 3) Refresh balances (source of truth for shares)
    m.up_filled_shares = await check_token_balance_async(client, m.up_token)
    m.dn_filled_shares = await check_token_balance_async(client, m.dn_token)

    rebalance_inventory_to_balance(m.up_fill_lots, m.up_filled_shares, cfg.entry_price, "UP", m.slug)
    rebalance_inventory_to_balance(m.dn_fill_lots, m.dn_filled_shares, cfg.entry_price, "DOWN", m.slug)

    # 4) Live order sizes
    m.up_live_buy_shares = _live_buy_size(m.up_orders)
    m.dn_live_buy_shares = _live_buy_size(m.dn_orders)
    m.up_live_sell_shares = _live_sell_size(m.up_orders)
    m.dn_live_sell_shares = _live_sell_size(m.dn_orders)

    # 5) Compute paired and unpaired
    m.paired_shares = min(m.up_filled_shares, m.dn_filled_shares)
    m.unpaired_up = max(0.0, m.up_filled_shares - m.dn_filled_shares)
    m.unpaired_dn = max(0.0, m.dn_filled_shares - m.up_filled_shares)

    target = cfg.shares_per_side
    if m.paired_shares < target or m.unpaired_up > 0 or m.unpaired_dn > 0:
        m.both_filled_cleanup_done = False

    # 6) Cost from fill lots (locked_cost is APPROXIMATE; fill lots use order limit price)
    m.up_total_cost = inventory_cost(m.up_fill_lots)
    m.dn_total_cost = inventory_cost(m.dn_fill_lots)
    paired_up_cost = cost_of_first_n_shares(m.up_fill_lots, m.paired_shares)
    paired_dn_cost = cost_of_first_n_shares(m.dn_fill_lots, m.paired_shares)
    m.locked_cost = paired_up_cost + paired_dn_cost
    m.locked_value = m.paired_shares * 1.0
    m.locked_edge = m.locked_value - m.locked_cost

    # 7) Position summary logging
    log.info(
        "  [%s] UP filled=%.1f DOWN filled=%.1f paired=%.1f unpaired_up=%.1f unpaired_dn=%.1f | "
        "UP live buy=%.1f DOWN live buy=%.1f | state=%s",
        m.slug[-20:], m.up_filled_shares, m.dn_filled_shares, m.paired_shares,
        m.unpaired_up, m.unpaired_dn, m.up_live_buy_shares, m.dn_live_buy_shares, m.state.value,
    )

    emit_event("market_snapshot", {
        "slug": m.slug,
        "state": m.state.value,
        "up_filled_shares": m.up_filled_shares,
        "dn_filled_shares": m.dn_filled_shares,
        "paired_shares": m.paired_shares,
        "unpaired_up": m.unpaired_up,
        "unpaired_dn": m.unpaired_dn,
        "locked_cost": m.locked_cost,
        "locked_value": m.locked_value,
        "locked_edge": m.locked_edge,
        "closed": m.closed,
        "redeemed": m.redeemed,
    })


# =========================================================
# STATE TRANSITIONS
# =========================================================

def _transition(m: MarketRuntime, new_state: MarketState) -> None:
    if m.state != new_state:
        old = m.state
        m.state = new_state
        m.last_transition_ts = int(time.time())
        emit_event("state_transition", {
            "slug": m.slug,
            "from": old.value if old else None,
            "to": new_state.value,
        })


# =========================================================
# STATE HANDLERS
# =========================================================

async def handle_watching(client, ws_feed: WSBookFeed, m: MarketRuntime, cfg: BotConfig) -> None:
    """No meaningful position; place fresh paired entry quotes if eligible."""
    if m.closed:
        return

    now_ts = int(time.time())
    if m.close_ts > 0 and (m.close_ts - now_ts) < cfg.entry_cutoff_seconds_before_close:
        emit_event("watch_skip_near_close", {"slug": m.slug, "secs_to_close": m.close_ts - now_ts})
        return

    up_ask = ws_feed.get_best_ask(m.up_token) if ws_feed else None
    dn_ask = ws_feed.get_best_ask(m.dn_token) if ws_feed else None
    if up_ask is None or dn_ask is None:
        up_ask = up_ask or await get_best_ask_sync(client, m.up_token)
        dn_ask = dn_ask or await get_best_ask_sync(client, m.dn_token)

    if up_ask is not None and dn_ask is not None:
        combined_ask = up_ask + dn_ask
        if combined_ask > cfg.max_combined_ask:
            emit_event("watch_skip_no_edge", {
                "slug": m.slug,
                "combined_ask": combined_ask,
                "threshold": cfg.max_combined_ask,
            })
            return

    missing_up = max(0.0, cfg.shares_per_side - m.up_filled_shares - m.up_live_buy_shares)
    missing_dn = max(0.0, cfg.shares_per_side - m.dn_filled_shares - m.dn_live_buy_shares)
    if missing_up <= 0 and missing_dn <= 0:
        return

    intended_notional = (missing_up + missing_dn) * cfg.entry_price
    live_entry_notional = (m.up_live_buy_shares + m.dn_live_buy_shares) * cfg.entry_price
    required_cash = intended_notional
    usdc = await check_usdc_balance_async(client)
    log.info("  Capital: usdc=%.2f, intended=%.2f, live_entry=%.2f", usdc, intended_notional, live_entry_notional)
    emit_event("watch_capital_check", {"slug": m.slug, "usdc": usdc, "required": required_cash, "live_entry_notional": live_entry_notional})
    if usdc >= 0 and usdc < required_cash:
        emit_event("watch_skip_no_usdc", {"slug": m.slug, "usdc": usdc, "required": required_cash})
        return

    if not cfg.enable_passive_entry:
        return

    can_post_up = up_ask is None or up_ask > cfg.entry_price
    can_post_dn = dn_ask is None or dn_ask > cfg.entry_price
    needs_both_sides = missing_up > 0 and missing_dn > 0

    if needs_both_sides and not (can_post_up and can_post_dn):
        emit_event(
            "watch_skip_unpaired_entry_risk",
            {
                "slug": m.slug,
                "missing_up": missing_up,
                "missing_dn": missing_dn,
                "best_up_ask": up_ask,
                "best_dn_ask": dn_ask,
                "entry_price": cfg.entry_price,
            },
        )
        return

    if missing_up > 0 and can_post_up:
        oid = await place_buy_limit(
            client, m.up_token, missing_up, cfg.entry_price,
            cfg.entry_expiry_seconds, "UP", cfg.dry_run,
            purpose="ENTRY", slug=m.slug, post_only=True,
        )
        if oid:
            m.last_action_ts = now_ts
    elif missing_up > 0:
        emit_event("watch_skip_crossing_up", {"slug": m.slug, "best_ask": up_ask, "entry_price": cfg.entry_price})

    if missing_dn > 0 and can_post_dn:
        oid = await place_buy_limit(
            client, m.dn_token, missing_dn, cfg.entry_price,
            cfg.entry_expiry_seconds, "DOWN", cfg.dry_run,
            purpose="ENTRY", slug=m.slug, post_only=True,
        )
        if oid:
            m.last_action_ts = now_ts
    elif missing_dn > 0:
        emit_event("watch_skip_crossing_down", {"slug": m.slug, "best_ask": dn_ask, "entry_price": cfg.entry_price})


async def handle_entry_orders_working(client, ws_feed: WSBookFeed, m: MarketRuntime, cfg: BotConfig, phase: str = "ACTIVE") -> None:
    """Orders are live; cancel stale ones after grace period (immediately when NEAR_EXPIRY)."""
    if phase != "NEAR_EXPIRY":
        now_ts = int(time.time())
        if now_ts - m.last_action_ts < cfg.max_one_sided_seconds:
            return

    await cancel_stale_entry_orders(client, m, cfg.dry_run)
    _transition(m, MarketState.WATCHING)


async def handle_one_sided_fill(client, ws_feed: WSBookFeed, m: MarketRuntime, cfg: BotConfig) -> None:
    """One side has more shares; emit event, allow grace period, then repair."""
    imbalance = max(m.unpaired_up, m.unpaired_dn)
    now_ts = int(time.time())
    phase = get_market_phase(m, now_ts, cfg.entry_cutoff_seconds_before_close)
    if imbalance < cfg.repair_trigger_shares and not (phase == "NEAR_EXPIRY" and imbalance >= cfg.must_flatten_shares_before_close):
        return

    emit_event("one_sided_fill", {
        "slug": m.slug,
        "unpaired_up": m.unpaired_up,
        "unpaired_dn": m.unpaired_dn,
        "paired_shares": m.paired_shares,
    })

    if phase != "NEAR_EXPIRY" and now_ts - m.last_transition_ts < cfg.max_one_sided_seconds:
        return

    _transition(m, MarketState.REPAIRING)
    await handle_repairing(client, ws_feed, m, cfg)


async def handle_repairing(client, ws_feed: WSBookFeed, m: MarketRuntime, cfg: BotConfig) -> None:
    """Try to restore balance: buy missing side or unwind."""
    now_ts = int(time.time())
    phase = get_market_phase(m, now_ts, cfg.entry_cutoff_seconds_before_close)
    # NEAR_EXPIRY: repair only if instantly profitable (stricter edge); else unwind
    effective_min_edge = max(0.0, cfg.min_repair_edge_per_share) if phase == "NEAR_EXPIRY" else cfg.min_repair_edge_per_share

    if m.repair_attempts >= cfg.max_repair_attempts:
        log.error("  REPAIR: max attempts %d exceeded for %s; marking ERROR", cfg.max_repair_attempts, m.slug)
        await cancel_market_orders_async(client, {m.up_token, m.dn_token}, cfg.dry_run)
        m.state = MarketState.ERROR
        m.errored = True
        emit_event("repair_max_attempts", {"slug": m.slug, "attempts": m.repair_attempts})
        return

    await cancel_stale_entry_orders(client, m, cfg.dry_run)

    up_ask = ws_feed.get_best_ask(m.up_token) if ws_feed else None
    dn_ask = ws_feed.get_best_ask(m.dn_token) if ws_feed else None
    if up_ask is None:
        up_ask = await get_best_ask_sync(client, m.up_token)
    if dn_ask is None:
        dn_ask = await get_best_ask_sync(client, m.dn_token)

    # Bail gate: if missing side is too expensive, unwind instead
    if cfg.bail_enabled:
        if m.unpaired_up > 0 and dn_ask is not None and dn_ask > cfg.bail_price:
            log.info("  REPAIR: DN ask %.2f > bail %.2f; unwinding UP", dn_ask, cfg.bail_price)
            _transition(m, MarketState.UNWINDING)
            await handle_unwinding(client, ws_feed, m, cfg)
            return
        if m.unpaired_dn > 0 and up_ask is not None and up_ask > cfg.bail_price:
            log.info("  REPAIR: UP ask %.2f > bail %.2f; unwinding DOWN", up_ask, cfg.bail_price)
            _transition(m, MarketState.UNWINDING)
            await handle_unwinding(client, ws_feed, m, cfg)
            return

    if m.unpaired_up > 0:
        repair_shares = m.unpaired_up
        if cfg.enable_aggressive_repair and dn_ask is not None:
            unmatched_up_cost = cost_of_last_n_shares(m.up_fill_lots, m.unpaired_up)
            repair_down_cost = repair_shares * dn_ask + estimate_taker_fee(repair_shares, dn_ask)
            total_paired_cost = unmatched_up_cost + repair_down_cost
            value = repair_shares * 1.0
            if value - total_paired_cost >= effective_min_edge * repair_shares:
                m.repair_attempts += 1
                await place_buy_fak(client, m.dn_token, repair_shares, dn_ask, "DOWN_REPAIR", cfg.dry_run, purpose="REPAIR", slug=m.slug)
                m.last_action_ts = int(time.time())
                return
        _transition(m, MarketState.UNWINDING)
        await handle_unwinding(client, ws_feed, m, cfg)
        return

    if m.unpaired_dn > 0:
        repair_shares = m.unpaired_dn
        if cfg.enable_aggressive_repair and up_ask is not None:
            unmatched_dn_cost = cost_of_last_n_shares(m.dn_fill_lots, m.unpaired_dn)
            repair_up_cost = repair_shares * up_ask + estimate_taker_fee(repair_shares, up_ask)
            total_paired_cost = unmatched_dn_cost + repair_up_cost
            value = repair_shares * 1.0
            if value - total_paired_cost >= effective_min_edge * repair_shares:
                m.repair_attempts += 1
                await place_buy_fak(client, m.up_token, repair_shares, up_ask, "UP_REPAIR", cfg.dry_run, purpose="REPAIR", slug=m.slug)
                m.last_action_ts = int(time.time())
                return
        _transition(m, MarketState.UNWINDING)
        await handle_unwinding(client, ws_feed, m, cfg)
        return

    if m.unpaired_up == 0 and m.unpaired_dn == 0:
        if m.paired_shares >= cfg.shares_per_side:
            _transition(m, MarketState.BOTH_FILLED)
        else:
            _transition(m, MarketState.WATCHING)


async def handle_both_filled(client, m: MarketRuntime, cfg: BotConfig) -> None:
    """Balanced target reached; cancel leftovers once, wait for resolution."""
    if not m.both_filled_cleanup_done:
        await cancel_market_orders_async(client, {m.up_token, m.dn_token}, cfg.dry_run)
        emit_event("both_filled", {
            "slug": m.slug,
            "paired_shares": m.paired_shares,
            "locked_cost": m.locked_cost,
            "locked_value": m.locked_value,
            "locked_edge": m.locked_edge,
        })
        m.both_filled_cleanup_done = True

    _transition(m, MarketState.WAIT_RESOLUTION)


async def handle_unwinding(client, ws_feed: WSBookFeed, m: MarketRuntime, cfg: BotConfig) -> None:
    """Sell unmatched inventory."""
    if m.unwind_attempts >= cfg.max_unwind_attempts:
        log.error("  UNWIND: max attempts %d exceeded for %s; marking ERROR", cfg.max_unwind_attempts, m.slug)
        await cancel_market_orders_async(client, {m.up_token, m.dn_token}, cfg.dry_run)
        m.state = MarketState.ERROR
        m.errored = True
        emit_event("unwind_max_attempts", {"slug": m.slug, "attempts": m.unwind_attempts})
        return

    if m.unpaired_up > 0:
        best_bid = ws_feed.get_best_bid(m.up_token) if ws_feed else None
        if best_bid is None:
            best_bid = await get_best_bid_sync(client, m.up_token)
        if best_bid is not None:
            unwind_cost = cost_of_last_n_shares(m.up_fill_lots, m.unpaired_up)
            unwind_fee = estimate_taker_fee(m.unpaired_up, best_bid)
            unwind_proceeds = (m.unpaired_up * best_bid) - unwind_fee
            approx_loss = unwind_cost - unwind_proceeds
            emit_event("unwind_started", {"slug": m.slug, "side": "UP", "shares": m.unpaired_up, "cost_basis": unwind_cost, "best_bid": best_bid, "taker_fee": unwind_fee, "approx_loss": approx_loss})
            log.info("  UNWIND: selling %.1f UP, cost=%.2f, bid=%.2f, approx_loss=%.2f", m.unpaired_up, unwind_cost, best_bid, approx_loss)
            m.unwind_attempts += 1
            await place_sell_fak(client, m.up_token, m.unpaired_up, best_bid, "UNWIND_UP", cfg.dry_run, purpose="UNWIND", slug=m.slug)
            m.last_action_ts = int(time.time())
            return

    if m.unpaired_dn > 0:
        best_bid = ws_feed.get_best_bid(m.dn_token) if ws_feed else None
        if best_bid is None:
            best_bid = await get_best_bid_sync(client, m.dn_token)
        if best_bid is not None:
            unwind_cost = cost_of_last_n_shares(m.dn_fill_lots, m.unpaired_dn)
            unwind_fee = estimate_taker_fee(m.unpaired_dn, best_bid)
            unwind_proceeds = (m.unpaired_dn * best_bid) - unwind_fee
            approx_loss = unwind_cost - unwind_proceeds
            emit_event("unwind_started", {"slug": m.slug, "side": "DOWN", "shares": m.unpaired_dn, "cost_basis": unwind_cost, "best_bid": best_bid, "taker_fee": unwind_fee, "approx_loss": approx_loss})
            log.info("  UNWIND: selling %.1f DOWN, cost=%.2f, bid=%.2f, approx_loss=%.2f", m.unpaired_dn, unwind_cost, best_bid, approx_loss)
            m.unwind_attempts += 1
            await place_sell_fak(client, m.dn_token, m.unpaired_dn, best_bid, "UNWIND_DOWN", cfg.dry_run, purpose="UNWIND", slug=m.slug)
            m.last_action_ts = int(time.time())
            return

    if m.unpaired_up == 0 and m.unpaired_dn == 0:
        if m.paired_shares >= cfg.shares_per_side:
            _transition(m, MarketState.WAIT_RESOLUTION)
        else:
            _transition(m, MarketState.WATCHING)


async def do_redeem(relayer, m: MarketRuntime) -> bool:
    """Redeem resolved paired inventory."""
    emit_event("redeem_started", {"slug": m.slug, "paired_shares": m.paired_shares})

    ok = await redeem_market(relayer, m.condition_id, m.neg_risk)

    if ok:
        m.redeemed = True
        _transition(m, MarketState.DONE)
        return True
    _transition(m, MarketState.ERROR)
    return False


async def advance_market_state(client, relayer, ws_feed: WSBookFeed, m: MarketRuntime, cfg: BotConfig) -> None:
    """Dispatch to next action based on reconciled truth."""
    if m.state == MarketState.DONE or m.state == MarketState.ERROR:
        return

    # If market resolved, redeem any position
    if m.closed:
        has_position = m.up_filled_shares > 0 or m.dn_filled_shares > 0
        if has_position and not m.redeemed and cfg.enable_redeem and relayer:
            _transition(m, MarketState.REDEEMING)
            await do_redeem(relayer, m)
            return
        if not has_position:
            _transition(m, MarketState.DONE)
            return

    target = cfg.shares_per_side
    now_ts = int(time.time())
    phase = get_market_phase(m, now_ts, cfg.entry_cutoff_seconds_before_close)

    # CASE A: fully balanced
    if m.paired_shares >= target and m.unpaired_up == 0 and m.unpaired_dn == 0:
        _transition(m, MarketState.BOTH_FILLED)
        await handle_both_filled(client, m, cfg)
        return

    # CASE B: imbalance (repair_trigger_shares normally; must_flatten near expiry)
    imbalance = max(m.unpaired_up, m.unpaired_dn)
    trigger_repair = imbalance >= cfg.repair_trigger_shares
    if phase == "NEAR_EXPIRY" and imbalance >= cfg.must_flatten_shares_before_close:
        trigger_repair = True
    if trigger_repair:
        _transition(m, MarketState.ONE_SIDED_FILL)
        await handle_one_sided_fill(client, ws_feed, m, cfg)
        return

    # CASE C: entry orders working
    if m.up_live_buy_shares > 0 or m.dn_live_buy_shares > 0:
        _transition(m, MarketState.ENTRY_ORDERS_WORKING)
        await handle_entry_orders_working(client, ws_feed, m, cfg, phase)
        return

    # CASE D: watching
    _transition(m, MarketState.WATCHING)
    await handle_watching(client, ws_feed, m, cfg)


# =========================================================
# RECOVERY
# =========================================================

def _merge_fill_lots_into_markets(tracked_markets: dict[int, MarketRuntime], fill_lots_by_slug: Dict[str, Dict[str, list]]) -> None:
    """Merge persisted fill lots into market runtimes."""
    for m in tracked_markets.values():
        lots = fill_lots_by_slug.get(m.slug)
        if lots:
            m.up_fill_lots = list(lots.get("up", []))
            m.dn_fill_lots = list(lots.get("dn", []))


async def recover_recent_markets(client, tracked_markets: dict[int, MarketRuntime], cfg: BotConfig) -> None:
    """On startup: scan recent markets, add to tracked_markets."""
    for ts in scan_recent_market_timestamps(cfg.max_recent_history_seconds):
        try:
            m = await fetch_market_runtime(ts, cfg)
            tracked_markets[ts] = m
        except Exception:
            continue


async def run():
    cfg = load_config()
    emit_event("status", {"message": "bot_starting", "config": asdict(cfg)})
    log.info("Initializing SDK...")
    client = init_clob_client()
    relayer = init_relayer() if cfg.enable_redeem else None

    log.info(
        "SDK ready. State machine bot: %.2fc entry, %s 15m markets, %.0f shares/side.",
        cfg.entry_price,
        cfg.asset.upper(),
        cfg.shares_per_side,
    )
    if relayer:
        log.info("Auto-redeem enabled (gasless relayer).")
    else:
        log.info("Auto-redeem DISABLED (no builder creds).")
    if cfg.dry_run:
        log.info("DRY RUN — no real orders will be placed.\n")
    else:
        log.info("")

    ws_feed = WSBookFeed()
    tracked_markets: dict[int, MarketRuntime] = {}

    fill_lots = load_persisted_state()
    await recover_recent_markets(client, tracked_markets, cfg)
    _merge_fill_lots_into_markets(tracked_markets, fill_lots)
    replay_fill_events_from_journal(tracked_markets)

    subscribed_tokens: set[str] = set()
    all_tokens = list(set(t for m in tracked_markets.values() for t in [m.up_token, m.dn_token]))
    if all_tokens:
        to_sub = [t for t in all_tokens[:20] if t not in subscribed_tokens]
        if to_sub:
            subscribed_tokens.update(to_sub)
            if not ws_feed.is_connected:
                await ws_feed.start(to_sub)
            else:
                await ws_feed.subscribe(to_sub)

    log.info("Tracking %d markets. Starting main loop.\n", len(tracked_markets))

    while True:
        now_ts = int(time.time())
        candidates = next_market_timestamps(now_ts)[:cfg.market_lookahead_count]

        for ts in candidates:
            if ts not in tracked_markets:
                try:
                    m = await fetch_market_runtime(ts, cfg)
                    tracked_markets[ts] = m
                    to_sub = [t for t in [m.up_token, m.dn_token] if t not in subscribed_tokens]
                    if to_sub:
                        subscribed_tokens.update(to_sub)
                        if ws_feed.is_connected:
                            await ws_feed.subscribe(to_sub)
                        else:
                            await ws_feed.start(to_sub)
                except Exception as e:
                    log.debug("Market %s not yet available: %s", ts, e)

        for ts, m in list(tracked_markets.items()):
            try:
                await reconcile_market_state(client, ws_feed, m, cfg)
                await advance_market_state(client, relayer, ws_feed, m, cfg)
            except Exception as e:
                m.state = MarketState.ERROR
                m.errored = True
                log.error("Market %s error: %s", m.slug, e)
                emit_event("market_error", {"slug": m.slug, "error": str(e)})

        save_persisted_state(_build_fill_lots_from_markets(tracked_markets))

        tracked_markets = {
            k: v for k, v in tracked_markets.items()
            if v.ts > now_ts - cfg.max_recent_history_seconds
            or v.state not in (MarketState.DONE, MarketState.ERROR)
        }

        await asyncio.sleep(cfg.recheck_interval_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("\nStopped.")
