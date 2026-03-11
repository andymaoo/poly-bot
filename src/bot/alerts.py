"""Telegram notification system for bot alerts.

Uses httpx async client to send messages via Telegram Bot API.
Gracefully degrades if credentials not configured.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.bot.types import FillEvent, MarketInfo, EdgeTier

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class AlertManager:
    """Sends notifications to Telegram chat."""

    def __init__(self, bot_token: str = "", chat_id: str = "", enabled: bool = True):
        self._enabled = enabled and bool(bot_token) and bool(chat_id)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._http: Optional[httpx.AsyncClient] = None
        self._last_update_id: int = 0

        if not self._enabled:
            logger.warning("Alerts disabled (no Telegram credentials)")

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def send(self, text: str) -> bool:
        """Send a message to Telegram. Returns False if disabled or failed."""
        if not self._enabled:
            return False
        try:
            url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
            resp = await self._get_http().post(url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False

    async def market_entry(self, market: MarketInfo, edge: EdgeTier, combined_ask: float) -> bool:
        text = (
            f"<b>ENTRY</b>\n"
            f"Edge: {edge.value.upper()} (combined ask: {combined_ask:.3f})\n"
            f"Condition: {market.condition_id[:16]}...\n"
            f"Tick: {market.tick_size}  Min size: {market.min_order_size}"
        )
        return await self.send(text)

    async def market_skipped(self, reason: str) -> bool:
        return await self.send(f"<b>SKIP</b>: {reason}")

    async def session_complete(
        self,
        fills: int,
        combined_vwap: float,
        risk_pnl: float,
        report_pnl: float,
        up_shares: float,
        down_shares: float,
    ) -> bool:
        text = (
            f"<b>SESSION DONE</b>\n"
            f"Fills: {fills}\n"
            f"Combined VWAP: {combined_vwap:.4f}\n"
            f"Risk PnL: ${risk_pnl:.2f}\n"
            f"Report PnL: ${report_pnl:.2f}\n"
            f"Shares: Up={up_shares:.1f} Down={down_shares:.1f}"
        )
        return await self.send(text)

    async def error(self, msg: str) -> bool:
        return await self.send(f"<b>ERROR</b>: {msg}")

    async def kill_switch(self, reason: str) -> bool:
        return await self.send(f"<b>KILL SWITCH</b>: {reason}")

    async def taker_fill_alert(self, fill: FillEvent) -> bool:
        text = (
            f"<b>TAKER FILL</b>\n"
            f"Side: {fill.side} | {fill.filled_shares:.1f} @ {fill.price:.2f}\n"
            f"Order: {fill.order_id[:12]}...\n"
            f"Investigate!"
        )
        return await self.send(text)

    async def daily_summary(self, pnl: float, markets: int, fill_rate: float) -> bool:
        text = (
            f"<b>DAILY SUMMARY</b>\n"
            f"PnL: ${pnl:.2f}\n"
            f"Markets: {markets}\n"
            f"Fill rate: {fill_rate:.1f}%"
        )
        return await self.send(text)

    async def heartbeat_status(self, phase: str, position_summary: str) -> bool:
        return await self.send(f"HB: {phase} | {position_summary}")

    # ── place_45-specific alerts ─────────────────────────────────────

    async def v2_entry(self, slug: str, edge: float, combined_ask: float,
                       price: float, shares: float, ev: float) -> bool:
        text = (
            f"<b>V2 ENTRY</b>\n"
            f"Market: <code>{slug}</code>\n"
            f"Edge: {edge:+.4f} (ask: {combined_ask:.3f})\n"
            f"Orders: {shares:.0f} x 2 @ {price:.2f}c\n"
            f"EV: {ev:.4f}  |  Cost: ${shares * price * 2:.2f}"
        )
        return await self.send(text)

    async def v2_fill(self, slug: str, side: str, shares: float,
                      price: float, both: bool, hedged_profit: float = 0) -> bool:
        if both:
            text = (
                f"<b>V2 BOTH FILLED</b>\n"
                f"Market: <code>{slug}</code>\n"
                f"Hedged profit: ${hedged_profit:.2f}"
            )
        else:
            text = (
                f"<b>V2 FILL</b> ({side})\n"
                f"Market: <code>{slug}</code>\n"
                f"Shares: {shares:.1f} @ {price:.2f}c"
            )
        return await self.send(text)

    async def v2_unwind(self, slug: str, trigger: str, side: str,
                        shares: float, exit_price: float, pnl: float) -> bool:
        text = (
            f"<b>V2 UNWIND</b>\n"
            f"Market: <code>{slug}</code>\n"
            f"Trigger: {trigger}\n"
            f"Sold: {shares:.1f} {side} @ {exit_price:.2f}\n"
            f"PnL: ${pnl:+.2f}"
        )
        return await self.send(text)

    async def v2_heartbeat(self, stats: dict) -> bool:
        text = (
            f"<b>V2 HEARTBEAT</b>\n"
            f"Entered: {stats.get('entered', 0)} | "
            f"Filled: {stats.get('filled', 0)} | "
            f"Both: {stats.get('both_filled', 0)}\n"
            f"No-fills: {stats.get('no_fills', 0)} | "
            f"Unwound: {stats.get('unwound', 0)}\n"
            f"PnL: ${stats.get('realized_pnl', 0):+.2f} | "
            f"Capital: ${stats.get('capital_deployed', 0):.2f}"
        )
        return await self.send(text)

    async def check_kill_command(self) -> bool:
        """Poll for /kill command in Telegram chat."""
        if not self._enabled:
            return False
        try:
            url = f"{TELEGRAM_API}/bot{self._bot_token}/getUpdates"
            params = {"offset": self._last_update_id + 1, "timeout": 0}
            resp = await self._get_http().get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            for update in data.get("result", []):
                self._last_update_id = update["update_id"]
                msg = update.get("message", {})
                # Only accept /kill from the configured chat
                if str(msg.get("chat", {}).get("id", "")) != self._chat_id:
                    continue
                text = msg.get("text", "")
                if text.strip() == "/kill":
                    logger.warning("Kill command received via Telegram")
                    return True
            return False
        except Exception as e:
            logger.error("Kill command check failed: %s", e)
            return False

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
