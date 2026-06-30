// WebSocket message types (matching backend schemas/ws.py)

export type WSEventType =
  | "orderbook_snapshot"
  | "orderbook_update"
  | "trade"
  | "order"
  | "balance"
  | "bulk_result"
  | "sl_tp_activated"
  | "ping"
  | "pong"
  | "auth_required"
  | "auth_ok"
  | "auth_failed"
  | "auth_timeout";

export interface WSMessage {
  event: WSEventType;
  ts: number;
  [key: string]: unknown;
}

export interface OrderBookSnapshot extends WSMessage {
  event: "orderbook_snapshot";
  symbol: string;
  bids: [number, number][]; // [price, volume]
  asks: [number, number][];
  last_trade_price: number | null;
}

export interface OrderBookChange {
  side: "bid" | "ask";
  price: number;
  qty: number;
}

export interface OrderBookUpdate extends WSMessage {
  event: "orderbook_update";
  symbol: string;
  changes: OrderBookChange[];
}

export interface TradePrint extends WSMessage {
  event: "trade";
  symbol: string;
  trade_id: number;
  price: number;
  quantity: number;
  side: string;
}

export interface OrderUpdate extends WSMessage {
  event: "order";
  order_id: number;
  symbol: string;
  side: string;
  type: string;
  status: string;
  status_event: string;
  price?: number;
  quantity?: number;
  filled_quantity?: number;
  remaining_quantity?: number;
  avg_fill_price?: number;
  client_order_id?: string;
  bulk_id?: string;
}

export interface BalanceUpdate extends WSMessage {
  event: "balance";
  asset: string;
  total: number;
  locked: number;
  available: number;
  change?: number;
  reason?: string;
  order_id?: number;
}

export interface BulkResult extends WSMessage {
  event: "bulk_result";
  bulk_id: string;
  action: string;
  total: number;
  succeeded: number;
  failed: { index: number; code: string; message: string }[];
}
