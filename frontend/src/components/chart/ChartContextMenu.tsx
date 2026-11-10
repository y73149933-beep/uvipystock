import { type CSSProperties } from "react";
import { placeChartOrder } from "./chartOrder";

export interface ChartMenuState {
  x: number;
  y: number;
  price: string;
}

interface ChartContextMenuProps {
  symbol: string;
  qty: string;
  menu: ChartMenuState;
  onDone: (msg: { text: string; ok: boolean }) => void;
  onClose: () => void;
}

interface Action {
  label: string;
  cls: string;
  req: () => { symbol: string; side: "buy" | "sell"; type: string; quantity: string; price?: string; stop_price?: string };
}

/** Right-click quick order menu. Price is taken from the exact click
 *  coordinate on the price scale (coordinateToPrice), rounded to tick. */
export function ChartContextMenu({ symbol, qty, menu, onDone, onClose }: ChartContextMenuProps) {
  const q = parseFloat(qty);
  const px = menu.price;

  const actions: Action[] = [
    { label: `Buy Limit @ ${px}`, cls: "text-bid", req: () => ({ symbol, side: "buy", type: "limit", quantity: String(q), price: px }) },
    { label: `Sell Limit @ ${px}`, cls: "text-ask", req: () => ({ symbol, side: "sell", type: "limit", quantity: String(q), price: px }) },
    { label: `Stop-Buy @ ${px}`, cls: "text-bid", req: () => ({ symbol, side: "buy", type: "stop_market", quantity: String(q), stop_price: px }) },
    { label: `Stop-Sell @ ${px}`, cls: "text-ask", req: () => ({ symbol, side: "sell", type: "stop_market", quantity: String(q), stop_price: px }) },
    { label: "Market Buy now", cls: "text-bid", req: () => ({ symbol, side: "buy", type: "market", quantity: String(q) }) },
    { label: "Market Sell now", cls: "text-ask", req: () => ({ symbol, side: "sell", type: "market", quantity: String(q) }) },
  ];

  const run = async (a: Action) => {
    onClose();
    if (!Number.isFinite(q) || q <= 0) {
      onDone({ text: "bad quantity", ok: false });
      return;
    }
    const r = await placeChartOrder(a.req() as never);
    onDone(r);
  };

  // keep the menu inside the chart pane
  const style: CSSProperties = {
    left: Math.max(2, menu.x),
    top: Math.max(2, menu.y),
  };

  return (
    <div
      className="absolute z-40 w-44 rounded border border-border bg-panelLight shadow-xl"
      style={style}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div className="border-b border-border px-2 py-1 text-[10px] uppercase tracking-wider text-gray-500">
        {symbol} @ {px}
      </div>
      {actions.map((a) => (
        <button
          key={a.label}
          onClick={() => void run(a)}
          className={`block w-full px-2 py-1.5 text-left text-[11px] hover:bg-panel ${a.cls}`}
        >
          {a.label}
        </button>
      ))}
      <button
        onClick={onClose}
        className="block w-full border-t border-border px-2 py-1 text-left text-[10px] text-gray-500 hover:bg-panel"
      >
        Cancel
      </button>
    </div>
  );
}
