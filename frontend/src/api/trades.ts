import { api } from "./client";
import type { Trade } from "@/types/trade";

interface TradeListResponse {
  trades: Trade[];
  pagination: { offset: number; limit: number; count: number };
}

interface PublicTrade {
  trade_id: number;
  symbol: string;
  price: number;
  quantity: number;
  side: string;
  ts: number;
}

interface PublicTradesResponse {
  trades: PublicTrade[];
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface CandlesResponse {
  candles: Candle[];
  symbol: string;
  timeframe: string;
}

export const tradesApi = {
  list: (params?: { symbol?: string; side?: string; offset?: number; limit?: number }) =>
    api.get<TradeListResponse>("/api/v1/trades", params),

  listPublic: (symbol: string, limit: number = 50) =>
    api.get<PublicTradesResponse>(`/api/v1/trades/public/${symbol}`),

  getCandles: (symbol: string, timeframe: string, limit: number = 500) =>
    api.get<CandlesResponse>(
      `/api/v1/trades/candles/${symbol}`,
      { timeframe, limit },
    ),
};
