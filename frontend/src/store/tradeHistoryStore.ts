import { create } from "zustand";
import type { Trade } from "@/types/trade";
import { tradesApi } from "@/api/trades";

const PAGE_SIZE = 50;

interface TradeHistoryState {
  trades: Trade[];
  loading: boolean;
  hasMore: boolean;
  loaded: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
}

let debounceTimer: ReturnType<typeof setTimeout> | null = null;

export const useTradeHistoryStore = create<TradeHistoryState>((set, get) => ({
  trades: [],
  loading: false,
  hasMore: false,
  loaded: false,
  error: null,

  refresh: async () => {
    set({ loading: true, error: null });
    try {
      const resp = await tradesApi.list({ limit: PAGE_SIZE, offset: 0 });
      set({
        trades: resp.trades,
        hasMore: resp.trades.length === PAGE_SIZE,
        loaded: true,
        loading: false,
      });
    } catch (err) {
      set({ loading: false, error: (err as Error).message });
    }
  },

  loadMore: async () => {
    const { trades, loading, hasMore } = get();
    if (loading || !hasMore) return;
    set({ loading: true });
    try {
      const resp = await tradesApi.list({ limit: PAGE_SIZE, offset: trades.length });
      set({
        trades: [...trades, ...resp.trades],
        hasMore: resp.trades.length === PAGE_SIZE,
        loading: false,
      });
    } catch (err) {
      set({ loading: false, error: (err as Error).message });
    }
  },
}));

/** Debounced refresh for WS fill bursts. */
export function scheduleMyTradesRefresh(delayMs = 900) {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    const st = useTradeHistoryStore.getState();
    if (st.loaded) void st.refresh();
  }, delayMs);
}
