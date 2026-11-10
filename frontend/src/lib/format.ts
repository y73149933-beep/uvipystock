import Decimal from "decimal.js";

/**
 * Format a number/string as a price with specified precision.
 */
export function formatPrice(value: string | number | null | undefined, precision: number = 2): string {
  if (value === null || value === undefined) return "—";
  try {
    const d = new Decimal(value);
    return d.toFixed(precision);
  } catch {
    return String(value);
  }
}

/**
 * Format a quantity with up to `maxPrecision` decimals, trimming trailing zeros.
 */
export function formatQty(value: string | number | null | undefined, maxPrecision: number = 8): string {
  if (value === null || value === undefined) return "—";
  try {
    const d = new Decimal(value);
    const fixed = d.toFixed(maxPrecision);
    // Trim trailing zeros but keep at least 1 decimal place
    return fixed.replace(/\.?0+$/, "") || "0";
  } catch {
    return String(value);
  }
}

/**
 * Format a USD-like value with thousands separators.
 */
/** Raw backend DECIMAL string -> human-editable form: strips trailing
 * fractional zeros without touching precision ("0.200...0" -> "0.2"). */
export function normalizeDecimal(value: string): string {
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}

export function formatUSD(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  try {
    const num = typeof value === "string" ? parseFloat(value) : value;
    if (isNaN(num)) return "—";
    return num.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  } catch {
    return String(value);
  }
}

/**
 * Format a percentage with specified decimals.
 */
export function formatPct(value: number, decimals: number = 2): string {
  return `${value.toFixed(decimals)}%`;
}

/**
 * Format a timestamp as HH:MM:SS (browser-local timezone).
 *
 * Canonical backend contract is epoch MILLISECONDS; values below 1e11 are
 * treated as unix SECONDS for backwards compatibility with older payloads.
 * ISO strings pass straight through to Date parsing.
 */
export function formatTime(ts: string | number): string {
  if (ts === undefined || ts === null || ts === "") return "—";
  let date: Date;
  if (typeof ts === "number") {
    date = new Date(ts < 1e11 ? ts * 1000 : ts);
  } else {
    date = new Date(ts);
  }
  if (isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("en-US", { hour12: false });
}

/**
 * Truncate a long string (e.g. order ID) for display.
 */
export function truncate(str: string, maxLen: number = 8): string {
  if (str.length <= maxLen) return str;
  return `${str.slice(0, maxLen)}…`;
}

/**
 * Format a signed change with + / - prefix.
 */
export function formatSigned(value: string | number, precision: number = 8): string {
  try {
    const d = new Decimal(value);
    const sign = d.gte(0) ? "+" : "";
    return sign + formatQty(value, precision);
  } catch {
    return String(value);
  }
}
