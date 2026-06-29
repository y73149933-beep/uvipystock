import { create } from "zustand";
import type { OrderBookSnapshotMsg, OrderBookUpdateMsg } from "@/ws/types";

interface OrderBookLevel {
  price: number;
  volume: number;
}

interface OrderBookState {
  symbol: string;
  bids: OrderBookLevel[]; // sorted descending by price
  asks: OrderBookLevel[]; // sorted ascending by price
  lastTradePrice: number | null;
  spread: number | null;
  setSymbol: (symbol: string) => void;
  applySnapshot: (msg: OrderBookSnapshotMsg) => void;
  applyUpdate: (msg: OrderBookUpdateMsg) => void;
  clear: () => void;
}

function aggregateLevels(changes: { side: "bid" | "ask"; price: number; qty: number }[]) {
  const bids = new Map<number, number>();
  const asks = new Map<number, number>();
  for (const c of changes) {
    const map = c.side === "bid" ? bids : asks;
    if (c.qty === 0) {
      map.delete(c.price);
    } else {
      map.set(c.price, c.qty);
    }
  }
  return { bids, asks };
}

export const useOrderbookStore = create<OrderBookState>((set, get) => ({
  symbol: "BTC/USDT",
  bids: [],
  asks: [],
  lastTradePrice: null,
  spread: null,

  setSymbol: (symbol) => {
    set({ symbol, bids: [], asks: [], lastTradePrice: null, spread: null });
  },

  applySnapshot: (msg) => {
    const bids = msg.bids.map(([price, vol]) => ({ price, volume: vol }));
    const asks = msg.asks.map(([price, vol]) => ({ price, volume: vol }));
    // Bids sorted descending, asks ascending
    bids.sort((a, b) => b.price - a.price);
    asks.sort((a, b) => a.price - b.price);
    const spread =
      bids.length > 0 && asks.length > 0 ? asks[0].price - bids[0].price : null;
    set({ bids, asks, lastTradePrice: msg.last_trade_price, spread });
  },

  applyUpdate: (msg) => {
    const { bids: bidChanges, asks: askChanges } = aggregateLevels(msg.changes);
    set((state) => {
      // Merge changes into existing levels
      const bidMap = new Map(state.bids.map((l) => [l.price, l.volume]));
      const askMap = new Map(state.asks.map((l) => [l.price, l.volume]));
      for (const [price, vol] of bidChanges) bidMap.set(price, vol);
      for (const [price, vol] of askChanges) askMap.set(price, vol);

      const bids = Array.from(bidMap.entries())
        .map(([price, volume]) => ({ price, volume }))
        .sort((a, b) => b.price - a.price);
      const asks = Array.from(askMap.entries())
        .map(([price, volume]) => ({ price, volume }))
        .sort((a, b) => a.price - b.price);

      const spread =
        bids.length > 0 && asks.length > 0 ? asks[0].price - bids[0].price : null;
      return { bids, asks, spread };
    });
  },

  clear: () => set({ bids: [], asks: [], lastTradePrice: null, spread: null }),
}));
