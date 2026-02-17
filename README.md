# poly-autobetting

Automated trading bot for Polymarket BTC 15-minute prediction markets. Places 45c limit orders on both UP and DOWN outcomes, monitors positions with real-time WebSocket prices, and auto-redeems resolved positions via gasless relayer.

## How It Works

The bot targets BTC 15-minute up/down binary markets on Polymarket. It places 45c buy limit orders on both sides (UP and DOWN) for each market. When both sides fill, the combined cost is $0.90 for a guaranteed $1.00 payout — netting $0.10 profit per round. It automatically rotates to new markets every 15 minutes.

- **Dual-Side 45c Orders** — places limit buys on both UP and DOWN at 45c each
- **Auto-Rotation** — detects and places orders on upcoming 15-minute markets
- **WebSocket Price Feed** — real-time order book monitoring with auto-reconnect
- **Auto-Redeem** — redeems resolved positions via Polymarket's gasless builder relayer

## Project Structure

```
scripts/
  place_45.py          # Main bot script
src/
  config.py            # Polymarket endpoints and fee config
  api/
    gamma.py           # Market discovery and metadata
    clob.py            # Order book and trade data
    data_api.py        # Data API client
    polygonscan.py     # Polygon chain queries
  bot/
    runner.py          # Orchestrator / event loop
    bot_config.py      # Configuration loader
    order_engine.py    # Order placement and management
    risk_engine.py     # Risk checks and circuit breakers
    ws_book_feed.py    # WebSocket order book feed
    fill_monitor.py    # Fill detection
    position_tracker.py # Position and P&L tracking
    session_loop.py    # Session primitives
    market_scheduler.py # Market rotation
    rebalance.py       # Position rebalancing
    state_manager.py   # State persistence
    math_engine.py     # Pricing utilities
    alerts.py          # Alert handling
    client.py          # CLOB SDK wrapper
    types.py           # Shared types
    backtest.py        # Backtesting
  analysis/            # Trade analysis and strategy evaluation
  monitor/             # Order book and spread monitoring
```

## Setup

### Prerequisites

- Python 3.10+
- Polymarket account with API credentials
- Funded Polygon wallet

### Installation

```bash
git clone https://github.com/0xalexkxk/poly-autobetting.git
cd poly-autobetting

python3 -m venv venv
source venv/bin/activate
pip install httpx python-dotenv py-clob-client py-builder-relayer-client web3 eth-abi
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Description |
|---|---|
| `POLYMARKET_PRIVATE_KEY` | Your Polygon wallet private key |
| `POLYMARKET_FUNDER` | Funder/proxy wallet address |
| `POLYMARKET_BUILDER_API_KEY` | Builder relayer API key (for gasless redeems) |
| `POLYMARKET_BUILDER_SECRET` | Builder relayer secret |
| `POLYMARKET_BUILDER_PASSPHRASE` | Builder relayer passphrase |
Builder relayer credentials are optional — without them, auto-redeem is disabled and you redeem manually.

## Usage

```bash
source venv/bin/activate
python scripts/place_45.py
```

The bot will place 45c limit orders on both UP and DOWN for the current and upcoming BTC 15-minute markets, then loop every 10 seconds to check for new markets and redeem resolved positions.

## Disclaimer

This software is for educational purposes. Trading on prediction markets involves risk. Use at your own discretion.
