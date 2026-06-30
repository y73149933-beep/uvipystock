import { create } from "zustand";
import { tradesApi } from "@/api/trades";

export interface Candle {
  time: number; // unix seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface ChartState {
  symbol: string;
  timeframe: string; // "1m", "5m", "15m", "1h", "4h", "1d"
  candles: Candle[];
  lastPrice: number | null;
  loading: boolean;
  setSymbol: (symbol: string) => void;
  setTimeframe: (tf: string) => void;
  setCandles: (candles: Candle[]) => void;
  updateLastPrice: (price: number) => void;
  addTradeToCandle: (price: number, qty: number) => void;
  fetchCandles: (symbol: string, timeframe: string) => Promise<void>;
}

function timeframeToSeconds(tf: string): number {
  const map: Record<string, number> = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400,
  };
  return map[tf] || 60;
}

export const useChartStore = create<ChartState>((set, get) => ({
  symbol: "BTC/USD",
  timeframe: "1m",
  candles: [],
  lastPrice: null,
  loading: false,

  setSymbol: (symbol) => set({ symbol, candles: [], lastPrice: null }),
  setTimeframe: (tf) => set({ timeframe: tf, candles: [] }),
  setCandles: (candles) => set({ candles }),

  fetchCandles: async (symbol: string, timeframe: string) => {
    set({ loading: true });
    try {
      const resp = await tradesApi.getCandles(symbol, timeframe, 500);
      set({ candles: resp.candles || [], loading: false });
    } catch {
      set({ loading: false, candles: [] });
    }
  },

  updateLastPrice: (price) => {
    set((state) => {
      if (state.candles.length === 0) {
        return { lastPrice: price };
      }
      const tf = timeframeToSeconds(state.timeframe);
      const now = Math.floor(Date.now() / 1000);
      const bucketTime = Math.floor(now / tf) * tf;
      const lastCandle = state.candles[state.candles.length - 1];

      if (lastCandle.time === bucketTime) {
        const updated = {
          ...lastCandle,
          close: price,
          high: Math.max(lastCandle.high, price),
          low: Math.min(lastCandle.low, price),
        };
        return {
          candles: [...state.candles.slice(0, -1), updated],
          lastPrice: price,
        };
      } else if (bucketTime > lastCandle.time) {
        const newCandle: Candle = {
          time: bucketTime,
          open: price,
          high: price,
          low: price,
          close: price,
          volume: 0,
        };
        return {
          candles: [...state.candles, newCandle].slice(-500),
          lastPrice: price,
        };
      }
      return { lastPrice: price };
    });
  },

  addTradeToCandle: (price, qty) => {
    set((state) => {
      if (state.candles.length === 0) return {};
      const tf = timeframeToSeconds(state.timeframe);
      const now = Math.floor(Date.now() / 1000);
      const bucketTime = Math.floor(now / tf) * tf;
      const lastCandle = state.candles[state.candles.length - 1];

      if (lastCandle.time === bucketTime) {
        const updated = {
          ...lastCandle,
          close: price,
          high: Math.max(lastCandle.high, price),
          low: Math.min(lastCandle.low, price),
          volume: lastCandle.volume + qty,
        };
        return {
          candles: [...state.candles.slice(0, -1), updated],
          lastPrice: price,
        };
      }
      return {};
    });
  },
}));
