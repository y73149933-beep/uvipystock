import { create } from "zustand";
import type { Position } from "@/types/position";
import { positionsApi } from "@/api/positions";

interface PositionState {
  positions: Position[];
  loading: boolean;
  error: string | null;
  fetchPositions: () => Promise<void>;
}

let debounceTimer: ReturnType<typeof setTimeout> | null = null;

export const usePositionStore = create<PositionState>((set) => ({
  positions: [],
  loading: false,
  error: null,

  fetchPositions: async () => {
    set({ loading: true, error: null });
    try {
      const resp = await positionsApi.list();
      set({ positions: resp.positions ?? [], loading: false });
    } catch (err) {
      set({ loading: false, error: (err as Error).message });
    }
  },
}));

/** Coalesced refresh for WS-triggered updates (fills arrive in bursts). */
export function schedulePositionsRefresh(delayMs = 800) {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    void usePositionStore.getState().fetchPositions();
  }, delayMs);
}
