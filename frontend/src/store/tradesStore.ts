import { create } from "zustand";
import type { TradePrintMsg } from "@/ws/types";

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
}

const MAX_RECENT = 50;

export const useTradesStore = create<TradesState>((set) => ({
  recentTrades: [],
  addTrade: (msg) => {
    set((state) => {
      const entry: TradeFeedEntry = {
        trade_id: msg.trade_id,
        price: msg.price,
        quantity: msg.quantity,
        side: msg.side,
        ts: msg.ts,
      };
      const next = [entry, ...state.recentTrades];
      return { recentTrades: next.slice(0, MAX_RECENT) };
    });
  },
  clear: () => set({ recentTrades: [] }),
}));
