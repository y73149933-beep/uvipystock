import { ordersApi } from "@/api/orders";
import type { OrderCreateRequest } from "@/types/order";

/** Shared one-click order sender for chart widgets. Returns a human result. */
export async function placeChartOrder(
  req: OrderCreateRequest,
): Promise<{ ok: boolean; text: string }> {
  try {
    const o = await ordersApi.create(req);
    const px = req.type === "market" ? "" : ` @ ${req.stop_price ?? req.price}`;
    return { ok: true, text: `#${o.id} ${req.side} ${req.type}${px} ×${req.quantity} → ${o.status}` };
  } catch (err) {
    const e = err as { message?: string; details?: unknown };
    return { ok: false, text: e.message ?? "order rejected" };
  }
}
