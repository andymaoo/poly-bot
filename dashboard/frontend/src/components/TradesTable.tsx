import React, { useMemo } from "react";
import type { EventEnvelope } from "../App";

type Props = {
  events: EventEnvelope[];
};

export const TradesTable: React.FC<Props> = ({ events }) => {
  const rows = useMemo(
    () =>
      events.filter((e) => e.type === "order_placed").slice(-200).reverse(),
    [events]
  );

  return (
    <section
      style={{
        borderRadius: 12,
        border: "1px solid #e5e7eb",
        padding: 12,
        backgroundColor: "white",
      }}
    >
      <h2 style={{ margin: 0, marginBottom: 8, fontSize: 14, fontWeight: 600 }}>Trades</h2>
      {rows.length === 0 ? (
        <div style={{ fontSize: 13, color: "#6b7280" }}>No trades yet.</div>
      ) : (
        <div style={{ maxHeight: 260, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid #e5e7eb" }}>
                <th>Time</th>
                <th>Side</th>
                <th>Shares</th>
                <th>Price</th>
                <th>Slug</th>
                <th>Order</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((ev, idx) => (
                <tr key={idx} style={{ borderBottom: "1px solid #f3f4f6" }}>
                  <td>{new Date(ev.ts * 1000).toLocaleTimeString()}</td>
                  <td>{ev.payload?.side}</td>
                  <td>{ev.payload?.shares}</td>
                  <td>{ev.payload?.price}</td>
                  <td>{ev.payload?.slug}</td>
                  <td style={{ fontFamily: "monospace" }}>
                    {String(ev.payload?.order_id || "").slice(0, 10)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
};

