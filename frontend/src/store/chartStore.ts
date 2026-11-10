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
  hasMore: boolean;   // server says older trades exist
  loadingOlder: boolean;
  olderCursor: number | null; // server-provided `next_end` for the next page
  setSymbol: (symbol: string) => void;
  setTimeframe: (tf: string) => void;
  setCandles: (candles: Candle[]) => void;
  updateLastPrice: (price: number, tsMs?: number) => void;
  addTradeToCandle: (price: number, qty: number, tsMs?: number) => void;
  fetchCandles: (symbol: string, timeframe: string) => Promise<void>;
  loadOlder: () => Promise<void>;
}

const MAX_CANDLES = 2000;

function timeframeToSeconds(tf: string): number {
  const map: Record<string, number> = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400,
  };
  return map[tf] || 60;
}

/** Bucket for a trade: prefer the server print timestamp (canonical,
 * matches the server-side kline cache bucketing exactly); fall back to
 * the client clock only when a feed message omits ts. */
function bucketAt(tf: string, tsMs?: number): number {
  const tfs = timeframeToSeconds(tf);
  const now = tsMs && tsMs > 0 ? tsMs / 1000 : Date.now() / 1000;
  return Math.floor(now / tfs) * tfs;
}

export const useChartStore = create<ChartState>((set, get) => ({
  symbol: "BTC/USD",
  timeframe: "1m",
  candles: [],
  lastPrice: null,
  loading: false,
  hasMore: false,
  loadingOlder: false,
  olderCursor: null,

  setSymbol: (symbol) =>
    set({ symbol, candles: [], lastPrice: null, hasMore: false, olderCursor: null }),
  setTimeframe: (tf) => set({ timeframe: tf, candles: [], hasMore: false, olderCursor: null }),
  setCandles: (candles) => set({ candles }),

  fetchCandles: async (symbol: string, timeframe: string) => {
    set({ loading: true });
    try {
      const resp = await tradesApi.getCandles(symbol, timeframe, 500);
      set({
        candles: resp.candles || [], loading: false,
        hasMore: !!resp.has_more, olderCursor: resp.next_end ?? null,
      });
    } catch {
      set({ loading: false, candles: [], hasMore: false, olderCursor: null });
    }
  },

  loadOlder: async () => {
    const { symbol, timeframe, candles, hasMore, loadingOlder, olderCursor } = get();
    if (!hasMore || loadingOlder || candles.length === 0) return;
    set({ loadingOlder: true });
    try {
      // Prefer the server's next_end cursor: it advances past empty
      // windows in sparse histories; fall back to our oldest candle.
      const cursor = olderCursor ?? candles[0].time;
      const resp = await tradesApi.getCandles(symbol, timeframe, 500, cursor);
      const older = resp.candles || [];
      if (older.length > 0) {
        set({ candles: [...older, ...candles].slice(-MAX_CANDLES) });
      }
      set({
        hasMore: !!resp.has_more, loadingOlder: false,
        olderCursor: resp.next_end ?? cursor,
      });
    } catch {
      set({ loadingOlder: false });
    }
  },

  updateLastPrice: (price, tsMs) => {
    set((state) => {
      const bucket = bucketAt(state.timeframe, tsMs);
      const lastCandle = state.candles[state.candles.length - 1];

      if (!lastCandle || bucket > lastCandle.time) {
        const newCandle: Candle = {
          time: bucket, open: price, high: price, low: price, close: price, volume: 0,
        };
        return { candles: [...state.candles, newCandle].slice(-MAX_CANDLES), lastPrice: price };
      }
      if (bucket === lastCandle.time) {
        const updated = {
          ...lastCandle,
          close: price,
          high: Math.max(lastCandle.high, price),
          low: Math.min(lastCandle.low, price),
        };
        return { candles: [...state.candles.slice(0, -1), updated], lastPrice: price };
      }
      // stale print (older bucket than our newest candle) — price only
      return { lastPrice: price };
    });
  },

  addTradeToCandle: (price, qty, tsMs) => {
    set((state) => {
      const bucket = bucketAt(state.timeframe, tsMs);
      const lastCandle = state.candles[state.candles.length - 1];

      if (!lastCandle) {
        // No history yet — start the first candles from real prints
        // (volume included), not from the orderbook fallback line.
        const newCandle: Candle = {
          time: bucket, open: price, high: price, low: price, close: price, volume: qty,
        };
        return { candles: [newCandle], lastPrice: price };
      }
      if (lastCandle.time === bucket) {
        const updated = {
          ...lastCandle,
          close: price,
          high: Math.max(lastCandle.high, price),
          low: Math.min(lastCandle.low, price),
          volume: lastCandle.volume + qty,
        };
        return { candles: [...state.candles.slice(0, -1), updated], lastPrice: price };
      }
      if (bucket > lastCandle.time) {
        // A new bucket opened with this trade. updateLastPrice normally
        // gets here first, but correctness must not depend on callback
        // order — create the bucket and fold the trade in.
        const newCandle: Candle = {
          time: bucket, open: price, high: price, low: price, close: price, volume: qty,
        };
        return { candles: [...state.candles, newCandle].slice(-MAX_CANDLES), lastPrice: price };
      }
      return {};
    });
  },
}));
