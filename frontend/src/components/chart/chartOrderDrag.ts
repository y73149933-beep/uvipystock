import type { Order, OrderCreateRequest } from "@/types/order";
import { ordersApi } from "@/api/orders";

export interface MyLevel {
  price: number;
  side: "buy" | "sell";
  /** remaining quantity resting on the book (iceberg: visible chunk) */
  qty: number;
  /** total already filled across the orders at this level */
  filled: number;
  kinds: Set<string>;
  orderIds: number[];
}

const RESTING = ["limit", "post_only", "iceberg"];

/** Aggregate the user's resting orders into price levels (shared by the
 *  overlay renderer and the drag hit-test so both agree on what is grabbable). */
export function buildMyLevels(orders: Order[], symbol: string): MyLevel[] {
  const map = new Map<string, MyLevel>();
  for (const o of orders) {
    if (o.symbol !== symbol) continue;
    if (o.status !== "new" && o.status !== "partially_filled") continue;
    if (!RESTING.includes(o.type) || o.price === null) continue;
    const p = Number(o.price);
    const rem = parseFloat(o.remaining_quantity);
    const fl = parseFloat(o.filled_quantity) || 0;
    if (!Number.isFinite(p) || p <= 0 || !Number.isFinite(rem) || rem <= 0) continue;
    const shown =
      o.type === "iceberg" && o.visible_quantity
        ? Math.min(rem, parseFloat(o.visible_quantity) || rem)
        : rem;
    const key = `${o.side}:${p}`;
    let cur = map.get(key);
    if (!cur) {
      cur = { price: p, side: o.side, qty: 0, filled: 0, kinds: new Set(), orderIds: [] };
      map.set(key, cur);
    }
    cur.qty += shown;
    cur.filled += fl;
    cur.kinds.add(o.type === "iceberg" ? "ICE" : o.type === "post_only" ? "PO" : "LMT");
    cur.orderIds.push(o.id);
  }
  return [...map.values()];
}

export interface MyStopLevel {
  /** trigger price (stop_price) */
  price: number;
  side: "buy" | "sell";
  qty: number;
  orderIds: number[];
  stopType: "stop_market" | "stop_limit";
}

const STOP_TYPES = ["stop_market", "stop_limit"];

/** Own conditional (stop) orders aggregated by side + trigger price. The
 *  overlay's amber dashed lines and the drag/menu hit-test must agree on
 *  what is grabbable, so this is the single source like buildMyLevels. */
export function buildMyStops(orders: Order[], symbol: string): MyStopLevel[] {
  const map = new Map<string, MyStopLevel>();
  for (const o of orders) {
    if (o.symbol !== symbol) continue;
    if (o.status !== "pending" && o.status !== "new" && o.status !== "partially_filled") continue;
    if (!(STOP_TYPES as readonly string[]).includes(o.type) || o.stop_price === null) continue;
    const p = Number(o.stop_price);
    const rem = parseFloat(o.remaining_quantity);
    if (!Number.isFinite(p) || p <= 0 || !Number.isFinite(rem) || rem <= 0) continue;
    const key = `${o.side}:${p}`;
    let cur = map.get(key);
    if (!cur) {
      cur = { price: p, side: o.side, qty: 0, orderIds: [], stopType: o.type as MyStopLevel["stopType"] };
      map.set(key, cur);
    }
    cur.qty += rem;
    cur.orderIds.push(o.id);
  }
  return [...map.values()];
}

export interface DragState {
  symbol: string;
  orderId: number;
  side: "buy" | "sell";
  oldPrice: number;
  newPrice: number;
  /** ghost is a stop trigger line, not a resting-order bar */
  isStop?: boolean;
}

/** Mutable singleton read by the overlay RAF loop — no React re-render per mousemove. */
export const dragState: { current: DragState | null } = { current: null };

/** Cancel-replace through the existing modify endpoint; returns a toast line. */
export async function applyDragModify(
  orderId: number,
  price: string,
  quantity: string,
): Promise<{ ok: boolean; text: string }> {
  try {
    const o = await ordersApi.modify(orderId, { price, quantity });
    return { ok: true, text: `#${orderId} moved -> #${o.id} @ ${o.price} (${o.status})` };
  } catch (err) {
    const e = err as { message?: string };
    return { ok: false, text: `modify #${orderId} failed: ${e.message ?? "error"}` };
  }
}

/** Cancel + re-create through the standard placement path (backend modify is
 *  limit-only by design). Not atomic, but a stop is not in the book either —
 *  the gap is milliseconds and the stop watcher re-registrations cover it. */
export async function applyStopReplace(
  o: Order,
  newStopPrice: string,
  quantity: string,
): Promise<{ ok: boolean; text: string }> {
  try {
    await ordersApi.cancel(o.id);
    const body: OrderCreateRequest = {
      symbol: o.symbol,
      side: o.side,
      type: o.type,
      stop_price: newStopPrice,
      quantity,
    };
    if (o.type === "stop_limit" && o.price) body.price = o.price;
    const n = await ordersApi.create(body);
    return { ok: true, text: `#${o.id} stop -> #${n.id} @ ${newStopPrice} (${n.status})` };
  } catch (err) {
    const e = err as { message?: string };
    return { ok: false, text: `stop replace #${o.id} failed: ${e.message ?? "error"}` };
  }
}
