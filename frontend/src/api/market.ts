import { publicApi } from "./client";

/** One aggregated row of the public conditional-order ladder. */
export interface StopLadderLevel {
  side: "buy" | "sell";
  type: string; // stop_market | stop_limit | trailing_stop
  price: string; // trigger price (exact Decimal string)
  volume: string; // aggregated remaining size
  count: number;
}

interface StopLadderResponse {
  symbol: string;
  levels: StopLadderLevel[];
  total_count: number;
  total_volume: string;
}

export const marketApi = {
  /** Anonymous — no auth headers needed for this endpoint. */
  getStopLadder: (symbol: string) =>
    publicApi.get<StopLadderResponse>("/api/v1/market/stops", { symbol }),
};
