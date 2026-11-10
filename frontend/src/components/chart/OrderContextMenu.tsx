import { useState } from "react";
import { useOrdersStore } from "@/store/ordersStore";
import { ordersApi } from "@/api/orders";
import { formatQty, normalizeDecimal } from "@/lib/format";
import type { MyLevel } from "./chartOrderDrag";
import type { Order } from "@/types/order";

interface OrderContextMenuProps {
  level: MyLevel;
  x: number;
  y: number;
  onClose: () => void;
  onDone: (m: { text: string; ok: boolean }) => void;
}

/** Click (no drag) on one of my violet bars: manage the order(s) at that
 *  price level without leaving the chart. */
export function OrderContextMenu({ level, x, y, onClose, onDone }: OrderContextMenuProps) {
  const openOrders = useOrdersStore((s) => s.openOrders);
  const setFocusOrder = useOrdersStore((s) => s.setFocusOrder);
  const [qty, setQty] = useState(() =>
    level.orderIds.length === 1
      ? normalizeDecimal(openOrders.find((o) => o.id === level.orderIds[0])?.remaining_quantity ?? "")
      : "",
  );

  const live = level.orderIds
    .map((id) => openOrders.find((o) => o.id === id))
    .filter((o): o is Order => !!o);

  if (live.length === 0) return null;

  const act = async (fn: () => Promise<string>) => {
    onClose();
    try {
      const text = await fn();
      onDone({ ok: true, text });
    } catch (err) {
      const e = err as { message?: string };
      onDone({ ok: false, text: e.message ?? "failed" });
    }
    void useOrdersStore.getState().fetchOpenOrders();
  };

  const cancelOne = (o: Order) =>
    act(async () => {
      await ordersApi.cancel(o.id);
      return `#${o.id} canceled`;
    });

  const cancelAll = () =>
    act(async () => {
      for (const o of live) await ordersApi.cancel(o.id);
      return `${live.length} orders @ ${level.price} canceled`;
    });

  const applyQty = () => {
    const q = parseFloat(qty);
    if (!Number.isFinite(q) || q <= 0) {
      onDone({ ok: false, text: "bad quantity" });
      return;
    }
    const o = live[0];
    void act(async () => {
      const n = await ordersApi.modify(o.id, { price: o.price as string, quantity: String(q) });
      return `#${o.id} qty -> #${n.id} ${n.quantity} @ ${n.price}`;
    });
  };

  return (
    <div
      className="absolute z-40 w-56 rounded border border-border bg-panelLight shadow-xl"
      style={{ left: Math.max(2, x), top: Math.max(2, y) }}
      onContextMenu={(e) => e.preventDefault()}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="border-b border-border px-2 py-1 text-[10px] uppercase tracking-wider text-gray-500">
        {live[0].symbol} · {level.price} · {live.length === 1 ? `#${live[0].id}` : `${live.length} orders`}
      </div>
      {live.map((o) => (
        <button
          key={o.id}
          onClick={() => void cancelOne(o)}
          className="block w-full px-2 py-1.5 text-left text-[11px] text-ask hover:bg-panel"
        >
          Cancel #{o.id}{" "}
          <span className="text-gray-500">
            {o.side} {o.type} {formatQty(o.remaining_quantity, 4)}
            {parseFloat(o.filled_quantity) > 0 ? ` (+${formatQty(o.filled_quantity, 4)})` : ""}
          </span>
        </button>
      ))}
      {live.length > 1 && (
        <button
          onClick={() => void cancelAll()}
          className="block w-full border-t border-border/50 px-2 py-1.5 text-left text-[11px] text-ask hover:bg-panel"
        >
          Cancel all @ {level.price}
        </button>
      )}
      {live.length === 1 && (
        <div className="flex items-center gap-1 border-t border-border/50 px-2 py-1.5">
          <input
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            step="0.0001"
            inputMode="decimal"
            title="New quantity (cancel-replace)"
            className="w-20 rounded border border-border bg-panel px-1.5 py-1 text-right font-mono text-[11px] text-gray-100 outline-none focus:border-accent"
          />
          <button
            onClick={applyQty}
            className="rounded bg-accent px-2 py-1 text-[10px] font-bold text-black"
          >
            Set qty
          </button>
        </div>
      )}
      <button
        onClick={() => {
          setFocusOrder(live[0].id);
          onClose();
        }}
        className="block w-full border-t border-border/50 px-2 py-1.5 text-left text-[11px] text-gray-300 hover:bg-panel"
      >
        Locate in Open Orders
      </button>
      <button
        onClick={onClose}
        className="block w-full border-t border-border px-2 py-1 text-left text-[10px] text-gray-500 hover:bg-panel"
      >
        Close
      </button>
    </div>
  );
}
