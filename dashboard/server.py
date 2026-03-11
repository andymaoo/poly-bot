import json
import os
import subprocess
import time
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .event_tailer import tail_events, load_events


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.jsonl")
STATIC_ROOT = os.path.join(PROJECT_ROOT, "dashboard", "static")
SYSTEMD_UNIT = "poly-autobetting.service"

app = FastAPI(title="Poly Autobetting Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConfigModel(BaseModel):
    price: float
    shares_per_side: float
    bail_price: float
    max_combined_ask: float
    order_expiry_seconds: int
    asset: str
    bail_enabled: bool


def read_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        raise HTTPException(status_code=500, detail="config.json not found")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def systemctl_cmd(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, check=False)


def get_bot_status() -> Dict[str, Any]:
    status_proc = systemctl_cmd(["is-active", SYSTEMD_UNIT])
    active = status_proc.stdout.strip()
    detail_proc = systemctl_cmd(["status", SYSTEMD_UNIT, "--no-pager"])
    return {
        "active": active,
        "status_raw": detail_proc.stdout,
        "error": detail_proc.stderr,
    }


@app.get("/api/status")
async def api_status() -> Dict[str, Any]:
    return get_bot_status()


@app.get("/api/config", response_model=ConfigModel)
async def api_get_config() -> ConfigModel:
    return ConfigModel(**read_config())


@app.put("/api/config", response_model=ConfigModel)
async def api_update_config(cfg: ConfigModel) -> ConfigModel:
    # Only allow config edits when bot is not running
    status = get_bot_status()
    if status["active"] == "active":
        raise HTTPException(status_code=400, detail="Bot must be stopped before changing config")
    write_config(cfg.dict())
    return cfg


@app.post("/api/bot/start")
async def api_bot_start() -> Dict[str, Any]:
    status = get_bot_status()
    if status["active"] == "active":
        return {"ok": True, "message": "Bot already running"}
    proc = systemctl_cmd(["start", SYSTEMD_UNIT])
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=proc.stderr or "Failed to start bot")
    time.sleep(1)
    return {"ok": True, "status": get_bot_status()}


@app.post("/api/bot/stop")
async def api_bot_stop() -> Dict[str, Any]:
    status = get_bot_status()
    if status["active"] != "active":
        return {"ok": True, "message": "Bot already stopped"}
    proc = systemctl_cmd(["stop", SYSTEMD_UNIT])
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=proc.stderr or "Failed to stop bot")
    time.sleep(1)
    return {"ok": True, "status": get_bot_status()}


@app.get("/api/trades")
async def api_trades(limit: int = 200) -> Dict[str, Any]:
    events = load_events(limit=limit, types=["order_placed", "redeemed", "bailed"])
    return {"events": events}


@app.get("/api/stats")
async def api_stats() -> Dict[str, Any]:
    events = load_events(limit=1000, types=None)
    today_ts_start = int(time.time()) - 24 * 3600
    stats: Dict[str, int] = {
        "orders_placed": 0,
        "markets_entered": 0,
        "redeems": 0,
        "skips": 0,
        "bailed": 0,
    }
    for ev in events:
        if ev.get("ts", 0) < today_ts_start:
            continue
        etype = ev.get("type")
        if etype == "order_placed":
            stats["orders_placed"] += 1
        elif etype == "market_entered":
            stats["markets_entered"] += 1
        elif etype in ("redeemed", "redeem_confirmed"):
            stats["redeems"] += 1
        elif etype == "market_skipped":
            stats["skips"] += 1
        elif etype == "bailed":
            stats["bailed"] += 1
    return stats


class ConnectionManager:
    def __init__(self) -> None:
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        async for event in tail_events():
            await manager.broadcast(event)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get("/", response_class=HTMLResponse)
async def index() -> Any:
    index_path = os.path.join(STATIC_ROOT, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    # Fallback simple page if frontend isn't built yet
    return HTMLResponse(
        """
        <html>
          <head><title>Poly Autobetting Dashboard</title></head>
          <body>
            <h1>Poly Autobetting Dashboard</h1>
            <p>Frontend not built yet. API is running.</p>
          </body>
        </html>
        """
    )

