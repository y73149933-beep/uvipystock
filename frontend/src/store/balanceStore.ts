import { create } from "zustand";
import type { Balance } from "@/types/balance";
import { balanceApi } from "@/api/balance";
import type { BalanceUpdateMsg } from "@/ws/types";

interface BalanceState {
  balances: Record<string, Balance>;
  loading: boolean;
  error: string | null;
  fetchBalances: () => Promise<void>;
  applyBalanceUpdate: (msg: BalanceUpdateMsg) => void;
  getAvailable: (asset: string) => string;
  getTotal: (asset: string) => string;
}

export const useBalanceStore = create<BalanceState>((set, get) => ({
  balances: {},
  loading: false,
  error: null,

  fetchBalances: async () => {
    set({ loading: true, error: null });
    try {
      const resp = await balanceApi.get();
      const map: Record<string, Balance> = {};
      for (const b of resp.balances) {
        map[b.asset] = b;
      }
      set({ balances: map, loading: false });
    } catch (err) {
      set({ loading: false, error: (err as Error).message });
    }
  },

  applyBalanceUpdate: (msg) => {
    set((state) => ({
      balances: {
        ...state.balances,
        [msg.asset]: {
          asset: msg.asset,
          total: String(msg.total),
          locked: String(msg.locked),
          available: String(msg.available),
          updated_at: new Date(msg.ts).toISOString(),
        },
      },
    }));
  },

  getAvailable: (asset) => {
    const b = get().balances[asset];
    return b?.available ?? "0";
  },

  getTotal: (asset) => {
    const b = get().balances[asset];
    return b?.total ?? "0";
  },
}));
