import { create } from "zustand";
import type { TradePrintMsg } from "@/ws/types";
import { tradesApi } from "@/api/trades";

interface TradeFeedEntry {
  trade_id: number;
  price: number;
  quantity: number;
  side: string;
  ts: number;
}

interface TradesState {
  recentTrades: TradeFeedEntry[];
  addTrade: (msg: TradePrintMsg) => void;
  clear: () => void;
  fetchRecent: (symbol: string) => Promise<void>;
}

const MAX_RECENT = 50;
const STORAGE_KEY = "exchange_recent_trades";

function loadFromStorage(): TradeFeedEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    return JSON.parse(raw);
  } catch {
    return [];
  }
}

function saveToStorage(trades: TradeFeedEntry[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(trades));
  } catch {
    // ignore
  }
}

export const useTradesStore = create<TradesState>((set) => ({
  recentTrades: loadFromStorage(),
  addTrade: (msg) => {
    set((state) => {
      if (msg.trade_id > 0 && state.recentTrades.some(t => t.trade_id === msg.trade_id)) {
        return {};
      }
      const entry: TradeFeedEntry = {
        trade_id: msg.trade_id,
        price: msg.price,
        quantity: msg.quantity,
        side: msg.side,
        // Legacy backend prints carry no ts — fall back to arrival time so
        // the feed never renders an invalid clock value.
        ts: typeof msg.ts === "number" ? msg.ts : Date.now(),
      };
      const next = [entry, ...state.recentTrades].slice(0, MAX_RECENT);
      saveToStorage(next);
      return { recentTrades: next };
    });
  },
  clear: () => {
    saveToStorage([]);
    set({ recentTrades: [] });
  },
  fetchRecent: async (symbol: string) => {
    try {
      const resp = await tradesApi.listPublic(symbol, MAX_RECENT);
      if (resp.trades && resp.trades.length > 0) {
        const entries: TradeFeedEntry[] = resp.trades.map(t => ({
          trade_id: t.trade_id,
          price: t.price,
          quantity: t.quantity,
          side: t.side,
          ts: t.ts,
        }));
        // the endpoint returns chronological order while the feed renders
        // first-item-on-top and WS adds prepend; without this the array
        // flips on every page load and the newest trades sink to the bottom
        entries.sort((a, b) => b.ts - a.ts);
        set({ recentTrades: entries });
        saveToStorage(entries);
      }
    } catch {
      // API call failed; silently ignore
    }
  },
}));
