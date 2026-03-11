import React from "react";

type Props = {
  status: "active" | "inactive" | "unknown";
  counters: {
    orders_placed: number;
    markets_entered: number;
    redeems: number;
    skips: number;
    bailed: number;
  };
  onStatusChange(next: "active" | "inactive" | "unknown"): void;
};

export const StatusHeader: React.FC<Props> = ({ status, counters, onStatusChange }) => {
  const start = async () => {
    const res = await fetch("/api/bot/start", { method: "POST" });
    if (res.ok) {
      onStatusChange("active");
    }
  };

  const stop = async () => {
    const res = await fetch("/api/bot/stop", { method: "POST" });
    if (res.ok) {
      onStatusChange("inactive");
    }
  };

  const color =
    status === "active" ? "#16a34a" : status === "inactive" ? "#6b7280" : "#f59e0b";

  return (
    <header
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 16,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div
          style={{
            width: 12,
            height: 12,
            borderRadius: "999px",
            backgroundColor: color,
          }}
        />
        <div>
          <div style={{ fontWeight: 600 }}>Poly Autobetting Bot</div>
          <div style={{ fontSize: 12, color: "#6b7280" }}>
            Status: {status === "unknown" ? "Checking..." : status === "active" ? "Running" : "Stopped"}
          </div>
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <div style={{ fontSize: 12, color: "#4b5563" }}>
          <span>Orders: {counters.orders_placed} · </span>
          <span>Markets: {counters.markets_entered} · </span>
          <span>Redeems: {counters.redeems} · </span>
          <span>Skips: {counters.skips} · </span>
          <span>Bailed: {counters.bailed}</span>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={start}
            disabled={status === "active"}
            style={{
              padding: "6px 12px",
              borderRadius: 6,
              border: "none",
              backgroundColor: status === "active" ? "#9ca3af" : "#16a34a",
              color: "white",
              fontSize: 13,
              cursor: status === "active" ? "default" : "pointer",
            }}
          >
            Start bot
          </button>
          <button
            onClick={stop}
            disabled={status !== "active"}
            style={{
              padding: "6px 12px",
              borderRadius: 6,
              border: "1px solid #ef4444",
              backgroundColor: "white",
              color: status === "active" ? "#ef4444" : "#9ca3af",
              fontSize: 13,
              cursor: status === "active" ? "pointer" : "default",
            }}
          >
            Stop bot
          </button>
        </div>
      </div>
    </header>
  );
};

