import React, { useMemo } from "react";
import type { EventEnvelope } from "../App";

type Props = {
  events: EventEnvelope[];
};

type Point = { t: number; value: number };

export const Charts: React.FC<Props> = ({ events }) => {
  const points = useMemo<Point[]>(() => {
    const result: Point[] = [];
    let cumulativeCost = 0;
    const sorted = [...events].sort((a, b) => a.ts - b.ts);
    for (const ev of sorted) {
      if (ev.type === "order_placed") {
        const p = ev.payload;
        const cost = (p.shares || 0) * (p.price || 0);
        cumulativeCost += cost;
        result.push({ t: ev.ts, value: cumulativeCost });
      }
    }
    return result;
  }, [events]);

  if (points.length === 0) {
    return (
      <section style={cardStyle}>
        <h2 style={titleStyle}>Activity</h2>
        <div style={{ fontSize: 13, color: "#6b7280" }}>No data yet.</div>
      </section>
    );
  }

  const minT = points[0].t;
  const maxT = points[points.length - 1].t;
  const minV = 0;
  const maxV = points[points.length - 1].value;

  const width = 400;
  const height = 160;

  const scaleX = (t: number) =>
    ((t - minT) / Math.max(1, maxT - minT)) * (width - 40) + 20;
  const scaleY = (v: number) =>
    height - ((v - minV) / Math.max(1, maxV - minV)) * (height - 30) - 10;

  const path = points
    .map((p, idx) => {
      const x = scaleX(p.t);
      const y = scaleY(p.value);
      return `${idx === 0 ? "M" : "L"}${x},${y}`;
    })
    .join(" ");

  return (
    <section style={cardStyle}>
      <h2 style={titleStyle}>Cumulative notional</h2>
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`}>
        <path d={path} fill="none" stroke="#2563eb" strokeWidth={2} />
      </svg>
      <div style={{ fontSize: 11, color: "#6b7280", marginTop: 4 }}>
        Rough cumulative cost of orders over time (not PnL).
      </div>
    </section>
  );
};

const cardStyle: React.CSSProperties = {
  borderRadius: 12,
  border: "1px solid #e5e7eb",
  padding: 12,
  backgroundColor: "white",
};

const titleStyle: React.CSSProperties = {
  margin: 0,
  marginBottom: 8,
  fontSize: 14,
  fontWeight: 600,
};

