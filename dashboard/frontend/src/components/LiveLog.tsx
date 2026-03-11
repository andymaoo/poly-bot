import React from "react";
import type { EventEnvelope } from "../App";

type Props = {
  events: EventEnvelope[];
};

export const LiveLog: React.FC<Props> = ({ events }) => {
  const last = events.slice(-100).reverse();

  return (
    <section
      style={{
        borderRadius: 12,
        border: "1px solid #e5e7eb",
        padding: 12,
        backgroundColor: "white",
        maxHeight: 280,
        overflow: "auto",
        fontSize: 12,
      }}
    >
      <h2 style={{ margin: 0, marginBottom: 8, fontSize: 14, fontWeight: 600 }}>Live feed</h2>
      {last.length === 0 ? (
        <div style={{ color: "#6b7280" }}>Waiting for events...</div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {last.map((ev, idx) => (
            <li key={idx} style={{ padding: "2px 0", borderBottom: "1px dashed #f3f4f6" }}>
              <span style={{ color: "#9ca3af", marginRight: 4 }}>
                {new Date(ev.ts * 1000).toLocaleTimeString()}
              </span>
              <span style={{ fontWeight: 500, marginRight: 4 }}>{ev.type}</span>
              <span style={{ color: "#4b5563" }}>
                {JSON.stringify(ev.payload)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
};

