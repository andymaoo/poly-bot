import React, { useEffect, useMemo, useState } from "react";
import { StatusHeader } from "./components/StatusHeader";
import { ConfigPanel } from "./components/ConfigPanel";
import { LiveLog } from "./components/LiveLog";
import { TradesTable } from "./components/TradesTable";
import { Charts } from "./components/Charts";

export type EventEnvelope = {
  type: string;
  ts: number;
  payload: any;
};

export type BotConfig = {
  price: number;
  shares_per_side: number;
  bail_price: number;
  max_combined_ask: number;
  order_expiry_seconds: number;
  asset: string;
  bail_enabled: boolean;
};

export const App: React.FC = () => {
  const [status, setStatus] = useState<"active" | "inactive" | "unknown">("unknown");
  const [config, setConfig] = useState<BotConfig | null>(null);
  const [events, setEvents] = useState<EventEnvelope[]>([]);

  const isRunning = status === "active";

  useEffect(() => {
    const fetchInitial = async () => {
      try {
        const [statusRes, configRes, tradesRes] = await Promise.all([
          fetch("/api/status"),
          fetch("/api/config"),
          fetch("/api/trades"),
        ]);
        const statusJson = await statusRes.json();
        const cfgJson = await configRes.json();
        const tradesJson = await tradesRes.json();
        setStatus(statusJson.active === "active" ? "active" : statusJson.active === "inactive" ? "inactive" : "unknown");
        setConfig(cfgJson);
        if (Array.isArray(tradesJson.events)) {
          setEvents(tradesJson.events);
        }
      } catch {
        // ignore for now
      }
    };
    fetchInitial();
  }, []);

  useEffect(() => {
    const wsProto = window.location.protocol === "https:" ? "wss" : "ws";
    const wsUrl = `${wsProto}://${window.location.host}/ws`;
    const ws = new WebSocket(wsUrl);
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as EventEnvelope;
        setEvents((prev) => [...prev.slice(-499), data]);
        if (data.type === "status" && data.payload?.message === "bot_starting") {
          setStatus("active");
        }
      } catch {
        // ignore
      }
    };
    ws.onclose = () => {
      // best-effort; no reconnect logic for now
    };
    return () => ws.close();
  }, []);

  const counters = useMemo(() => {
    const now = Date.now() / 1000;
    const dayAgo = now - 24 * 3600;
    const result = {
      orders_placed: 0,
      markets_entered: 0,
      redeems: 0,
      skips: 0,
      bailed: 0,
    };
    for (const ev of events) {
      if (ev.ts < dayAgo) continue;
      switch (ev.type) {
        case "order_placed":
          result.orders_placed += 1;
          break;
        case "market_entered":
          result.markets_entered += 1;
          break;
        case "redeemed":
        case "redeem_confirmed":
          result.redeems += 1;
          break;
        case "market_skipped":
          result.skips += 1;
          break;
        case "bailed":
          result.bailed += 1;
          break;
      }
    }
    return result;
  }, [events]);

  return (
    <div style={{ fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif", padding: 16 }}>
      <StatusHeader status={status} counters={counters} onStatusChange={setStatus} />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 380px) 1fr", gap: 16, marginTop: 16 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <ConfigPanel config={config} disabled={isRunning} onConfigChange={setConfig} />
          <LiveLog events={events} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <TradesTable events={events} />
          <Charts events={events} />
        </div>
      </div>
    </div>
  );
};

