import { api } from "./client";

export interface PublicTradingPair {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  price_precision: number;
  quantity_precision: number;
  min_lot_size: string;
  max_lot_size: string;
  tick_size: string;
  is_active: boolean;
}

export const pairsApi = {
  list: () => api.get<PublicTradingPair[]>("/api/v1/pairs"),
};
