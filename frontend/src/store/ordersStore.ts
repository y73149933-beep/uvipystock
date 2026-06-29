import { create } from "zustand";
import type { Order } from "@/types/order";
import { ordersApi } from "@/api/orders";
import type { OrderUpdateMsg } from "@/ws/types";

interface OrdersState {
  openOrders: Order[];
  allOrders: Order[]; // includes filled/canceled for history
  loading: boolean;
  error: string | null;
  selectedIds: Set<number>;
  fetchOpenOrders: () => Promise<void>;
  applyOrderUpdate: (msg: OrderUpdateMsg) => void;
  toggleSelect: (id: number) => void;
  selectAll: () => void;
  clearSelection: () => void;
  cancelSelected: () => Promise<void>;
  cancelAll: (symbol?: string) => Promise<void>;
}

export const useOrdersStore = create<OrdersState>((set, get) => ({
  openOrders: [],
  allOrders: [],
  loading: false,
  error: null,
  selectedIds: new Set(),

  fetchOpenOrders: async () => {
    set({ loading: true, error: null });
    try {
      const resp = await ordersApi.list({ status: "new,partially_filled,pending" });
      set({ openOrders: resp.orders, loading: false });
    } catch (err) {
      set({ loading: false, error: (err as Error).message });
    }
  },

  applyOrderUpdate: (msg) => {
    set((state) => {
      const orderId = msg.order_id;
      const existing = state.openOrders.find((o) => o.id === orderId);
      const status = msg.status as Order["status"];

      // If the order is now terminal (filled/canceled/rejected/expired),
      // remove it from openOrders
      const isTerminal = ["filled", "canceled", "rejected", "expired"].includes(status);

      if (isTerminal) {
        return {
          openOrders: state.openOrders.filter((o) => o.id !== orderId),
        };
      }

      // Update existing or add new
      if (existing) {
        return {
          openOrders: state.openOrders.map((o) =>
            o.id === orderId
              ? {
                  ...o,
                  status,
                  filled_quantity: msg.filled_quantity ? String(msg.filled_quantity) : o.filled_quantity,
                  remaining_quantity: msg.remaining_quantity ? String(msg.remaining_quantity) : o.remaining_quantity,
                  avg_fill_price: msg.avg_fill_price ? String(msg.avg_fill_price) : o.avg_fill_price,
                }
              : o,
          ),
        };
      } else {
        // New order (placed via WS event)
        const newOrder: Order = {
          id: orderId,
          user_id: 0,
          symbol: msg.symbol,
          side: msg.side as Order["side"],
          type: msg.type as Order["type"],
          status,
          price: msg.price ? String(msg.price) : null,
          stop_price: null,
          trailing_delta: null,
          quantity: msg.quantity ? String(msg.quantity) : "0",
          filled_quantity: msg.filled_quantity ? String(msg.filled_quantity) : "0",
          filled_quote_qty: "0",
          remaining_quantity: msg.remaining_quantity ? String(msg.remaining_quantity) : msg.quantity ? String(msg.quantity) : "0",
          avg_fill_price: msg.avg_fill_price ? String(msg.avg_fill_price) : null,
          visible_quantity: null,
          hidden_quantity: null,
          parent_order_id: null,
          sl_order_id: null,
          tp_order_id: null,
          replaces_id: null,
          replaced_by_id: null,
          bulk_id: msg.bulk_id ?? null,
          replace_count: 0,
          created_at: new Date(msg.ts).toISOString(),
          updated_at: new Date(msg.ts).toISOString(),
        };
        return { openOrders: [newOrder, ...state.openOrders] };
      }
    });
  },

  toggleSelect: (id) => {
    set((state) => {
      const next = new Set(state.selectedIds);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return { selectedIds: next };
    });
  },

  selectAll: () => {
    set((state) => ({
      selectedIds: new Set(state.openOrders.map((o) => o.id)),
    }));
  },

  clearSelection: () => set({ selectedIds: new Set() }),

  cancelSelected: async () => {
    const ids = Array.from(get().selectedIds);
    if (ids.length === 0) return;
    try {
      await ordersApi.bulkCancel({ order_ids: ids });
      set({ selectedIds: new Set() });
      // Orders will be removed from openOrders via WS events
    } catch (err) {
      set({ error: (err as Error).message });
    }
  },

  cancelAll: async (symbol) => {
    try {
      await ordersApi.bulkCancel({ cancel_all: true, symbol });
      set({ selectedIds: new Set() });
    } catch (err) {
      set({ error: (err as Error).message });
    }
  },
}));
