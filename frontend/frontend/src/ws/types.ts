// Shared WS message types for the frontend

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

export interface WSMessageBase {
  event: WSEventType;
  ts: number;
}

export interface OrderBookSnapshotMsg extends WSMessageBase {
  event: "orderbook_snapshot";
  symbol: string;
  bids: [number, number][];
  asks: [number, number][];
  last_trade_price: number | null;
}

export interface OrderBookChange {
  side: "bid" | "ask";
  price: number;
  qty: number;
}

export interface OrderBookUpdateMsg extends WSMessageBase {
  event: "orderbook_update";
  symbol: string;
  changes: OrderBookChange[];
}

export interface TradePrintMsg extends WSMessageBase {
  event: "trade";
  symbol: string;
  trade_id: number;
  price: number;
  quantity: number;
  side: string;
}

export interface OrderUpdateMsg extends WSMessageBase {
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

export interface BalanceUpdateMsg extends WSMessageBase {
  event: "balance";
  asset: string;
  total: number;
  locked: number;
  available: number;
  change?: number;
  reason?: string;
  order_id?: number;
}

export interface BulkResultMsg extends WSMessageBase {
  event: "bulk_result";
  bulk_id: string;
  action: string;
  total: number;
  succeeded: number;
  failed: { index: number; code: string; message: string }[];
}

export type WSMessage =
  | OrderBookSnapshotMsg
  | OrderBookUpdateMsg
  | TradePrintMsg
  | OrderUpdateMsg
  | BalanceUpdateMsg
  | BulkResultMsg
  | WSMessageBase;

export interface WSAuthMessage {
  action: "auth";
  api_key: string;
  timestamp: number;
  signature: string;
}
