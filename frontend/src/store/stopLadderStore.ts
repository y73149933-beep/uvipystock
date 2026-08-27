import { create } from "zustand";
import { marketApi, type StopLadderLevel } from "@/api/market";

/**
 * Public conditional-order ladder ("stop book"):
 * aggregated, anonymized trigger levels of ALL users' pending stops.
 *
 * Refreshed on mount / symbol change / `stops_changed` WS event and by a
 * slow safety-net poll (WS invalidation is best-effort pub/sub).
 */

interface StopLadderState {
  levels: StopLadderLevel[];
  totalCount: number;
  totalVolume: string;
  fetchedSymbol: string | null;
  loading: boolean;
  error: string | null;
  fetchLadder: (symbol: string) => Promise<void>;
  clear: () => void;
}

/** Symbol of the most recent in-flight request (stale-response guard). */
let lastRequested: string | null = null;

export const useStopLadderStore = create<StopLadderState>((set) => ({
  levels: [],
  totalCount: 0,
  totalVolume: "0",
  fetchedSymbol: null,
  loading: false,
  error: null,

  fetchLadder: async (symbol) => {
    lastRequested = symbol;
    set({ loading: true });
    try {
      const resp = await marketApi.getStopLadder(symbol);
      // Drop stale response after a quick symbol switch
      if (lastRequested !== symbol) return;
      set({
        levels: resp.levels,
        totalCount: resp.total_count,
        totalVolume: resp.total_volume,
        fetchedSymbol: symbol,
        loading: false,
        error: null,
      });
    } catch (err) {
      if (lastRequested !== symbol) return;
      set({ loading: false, error: (err as Error).message });
    }
  },

  clear: () =>
    set({
      levels: [],
      totalCount: 0,
      totalVolume: "0",
      fetchedSymbol: null,
      error: null,
    }),
}));
