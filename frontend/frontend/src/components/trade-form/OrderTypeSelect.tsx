import type { OrderType } from "@/types/order";

const ORDER_TYPES: { value: OrderType; label: string }[] = [
  { value: "limit", label: "Limit" },
  { value: "market", label: "Market" },
  { value: "stop_market", label: "Stop-Market" },
  { value: "stop_limit", label: "Stop-Limit" },
  { value: "post_only", label: "Post-Only" },
  { value: "ioc", label: "IOC" },
  { value: "fok", label: "FOK" },
  { value: "iceberg", label: "Iceberg" },
  { value: "trailing_stop", label: "Trailing" },
];

interface OrderTypeSelectProps {
  value: OrderType;
  onChange: (type: OrderType) => void;
}

export function OrderTypeSelect({ value, onChange }: OrderTypeSelectProps) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as OrderType)}
      className="w-full rounded border border-border bg-panel px-3 py-2 text-sm text-gray-100 focus:border-accent focus:outline-none"
    >
      {ORDER_TYPES.map((t) => (
        <option key={t.value} value={t.value}>
          {t.label}
        </option>
      ))}
    </select>
  );
}
