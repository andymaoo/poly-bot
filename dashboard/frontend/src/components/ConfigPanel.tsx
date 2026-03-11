import React, { useState } from "react";
import type { BotConfig } from "../App";

type Props = {
  config: BotConfig | null;
  disabled: boolean;
  onConfigChange(next: BotConfig): void;
};

export const ConfigPanel: React.FC<Props> = ({ config, disabled, onConfigChange }) => {
  const [local, setLocal] = useState<BotConfig | null>(config);
  const [saving, setSaving] = useState(false);

  React.useEffect(() => {
    setLocal(config);
  }, [config]);

  if (!local) {
    return (
      <section style={cardStyle}>
        <h2 style={titleStyle}>Config</h2>
        <div style={{ fontSize: 13, color: "#6b7280" }}>Loading config...</div>
      </section>
    );
  }

  const updateField = (key: keyof BotConfig, value: any) => {
    const next = { ...local, [key]: value };
    setLocal(next);
  };

  const save = async () => {
    if (!local) return;
    setSaving(true);
    try {
      const res = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(local),
      });
      if (res.ok) {
        const json = await res.json();
        onConfigChange(json);
      } else {
        const text = await res.text();
        alert("Failed to save config: " + text);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <section style={cardStyle}>
      <h2 style={titleStyle}>Config</h2>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, fontSize: 13 }}>
        <Field
          label="Price"
          type="number"
          value={local.price}
          disabled={disabled}
          onChange={(v) => updateField("price", Number(v))}
        />
        <Field
          label="Shares per side"
          type="number"
          value={local.shares_per_side}
          disabled={disabled}
          onChange={(v) => updateField("shares_per_side", Number(v))}
        />
        <Field
          label="Bail price"
          type="number"
          value={local.bail_price}
          disabled={disabled}
          onChange={(v) => updateField("bail_price", Number(v))}
        />
        <Field
          label="Max combined ask"
          type="number"
          value={local.max_combined_ask}
          disabled={disabled}
          onChange={(v) => updateField("max_combined_ask", Number(v))}
        />
        <Field
          label="Order expiry (s)"
          type="number"
          value={local.order_expiry_seconds}
          disabled={disabled}
          onChange={(v) => updateField("order_expiry_seconds", Number(v))}
        />
        <Field
          label="Asset"
          type="text"
          value={local.asset}
          disabled={disabled}
          onChange={(v) => updateField("asset", v)}
        />
        <label style={{ display: "flex", alignItems: "center", gap: 6, gridColumn: "1 / span 2" }}>
          <input
            type="checkbox"
            checked={local.bail_enabled}
            disabled={disabled}
            onChange={(e) => updateField("bail_enabled", e.target.checked)}
          />
          <span>Bail-out enabled</span>
        </label>
      </div>
      <button
        onClick={save}
        disabled={disabled || saving}
        style={{
          marginTop: 10,
          padding: "6px 12px",
          borderRadius: 6,
          border: "none",
          backgroundColor: disabled ? "#9ca3af" : "#2563eb",
          color: "white",
          fontSize: 13,
          cursor: disabled ? "default" : "pointer",
        }}
      >
        Save config
      </button>
      {disabled && (
        <div style={{ marginTop: 4, fontSize: 11, color: "#6b7280" }}>
          Stop the bot to edit configuration.
        </div>
      )}
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

type FieldProps = {
  label: string;
  value: string | number;
  type: "text" | "number";
  disabled: boolean;
  onChange(v: string): void;
};

const Field: React.FC<FieldProps> = ({ label, value, type, disabled, onChange }) => (
  <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
    <span style={{ color: "#4b5563", fontSize: 12 }}>{label}</span>
    <input
      type={type}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      style={{
        padding: "4px 8px",
        borderRadius: 6,
        border: "1px solid #d1d5db",
        fontSize: 13,
      }}
    />
  </label>
);

