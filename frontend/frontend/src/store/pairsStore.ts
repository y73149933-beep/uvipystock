import { create } from "zustand";
import { pairsApi, type PublicTradingPair } from "@/api/pairs";

interface PairsState {
  pairs: PublicTradingPair[];
  symbols: string[];
  loading: boolean;
  fetchPairs: () => Promise<void>;
}

export const usePairsStore = create<PairsState>((set) => ({
  pairs: [],
  symbols: ["BTC/USDT"], // default until fetched
  loading: false,
  fetchPairs: async () => {
    set({ loading: true });
    try {
      const resp = await pairsApi.list();
      const symbols = resp.map((p) => p.symbol);
      set({ pairs: resp, symbols, loading: false });
    } catch {
      set({ loading: false });
    }
  },
}));
