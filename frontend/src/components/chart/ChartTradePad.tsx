import { useState } from "react";
import { placeChartOrder } from "./chartOrder";

interface ChartTradePadProps {
  symbol: string;
  qty: string;
  setQty: (v: string) => void;
  msg: { text: string; ok: boolean } | null;
  setMsg: (m: { text: string; ok: boolean } | null) => void;
}

/** Pinned one-click market buttons on the chart. Fires immediately —
 *  that is the point of one-click trading; the qty field is the guard. */
export function ChartTradePad({ symbol, qty, setQty, msg, setMsg }: ChartTradePadProps) {
  const [busy, setBusy] = useState(false);

  const fire = async (side: "buy" | "sell") => {
    const q = parseFloat(qty);
    if (!Number.isFinite(q) || q <= 0) {
      setMsg({ text: "bad quantity", ok: false });
      return;
    }
    setBusy(true);
    const r = await placeChartOrder({ symbol, side, type: "market", quantity: String(q) });
    setBusy(false);
    setMsg(r);
  };

  return (
    <>
      {msg && (
        <div
          className={`absolute right-20 top-11 z-30 max-w-[260px] truncate rounded border border-border bg-panelLight/90 px-2 py-0.5 text-[10px] backdrop-blur-sm ${
            msg.ok ? "text-emerald-400" : "text-red-400"
          }`}
        >
          {msg.text}
        </div>
      )}
      <div className="absolute right-20 top-2 z-30 flex items-center gap-1 rounded border border-border bg-panelLight/85 px-1.5 py-1 backdrop-blur-sm">
        <input
          value={qty}
          onChange={(e) => setQty(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          step="0.0001"
          inputMode="decimal"
          title="Order size (base asset) — used by these buttons AND the right-click menu"
          className="w-16 rounded border border-border bg-panel px-1.5 py-1 text-right font-mono text-[11px] text-gray-100 outline-none focus:border-accent"
        />
        <button
          onClick={() => void fire("buy")}
          disabled={busy}
          className="rounded px-2 py-1 text-[11px] font-bold text-black hover:opacity-90 disabled:opacity-50"
          style={{ backgroundColor: "#22c55e" }}
        >
          Buy
        </button>
        <button
          onClick={() => void fire("sell")}
          disabled={busy}
          className="rounded px-2 py-1 text-[11px] font-bold text-black hover:opacity-90 disabled:opacity-50"
          style={{ backgroundColor: "#ef4444" }}
        >
          Sell
        </button>
      </div>
    </>
  );
}
