// Domain types matching the backend Pydantic schemas

export type OrderSide = "buy" | "sell";

export type OrderType =
  | "market"
  | "limit"
  | "stop_market"
  | "stop_limit"
  | "post_only"
  | "ioc"
  | "fok"
  | "trailing_stop"
  | "iceberg";

export type OrderStatus =
  | "pending"
  | "new"
  | "partially_filled"
  | "filled"
  | "canceled"
  | "rejected"
  | "expired";

export type TimeInForce = "gtc" | "ioc" | "fok" | "post_only";

export interface SLTPConfig {
  type: "stop_market" | "stop_limit" | "limit";
  stop_price?: string;
  price?: string;
  quantity?: string;
}

export interface Order {
  id: number;
  user_id: number;
  symbol: string;
  side: OrderSide;
  type: OrderType;
  status: OrderStatus;
  price: string | null;
  stop_price: string | null;
  trailing_delta: string | null;
  quantity: string;
  filled_quantity: string;
  filled_quote_qty: string;
  remaining_quantity: string;
  avg_fill_price: string | null;
  visible_quantity: string | null;
  hidden_quantity: string | null;
  parent_order_id: number | null;
  sl_order_id: number | null;
  tp_order_id: number | null;
  replaces_id: number | null;
  replaced_by_id: number | null;
  bulk_id: string | null;
  replace_count: number;
  created_at: string;
  updated_at: string;
}

export interface OrderCreateRequest {
  symbol: string;
  side: OrderSide;
  type: OrderType;
  price?: string;
  stop_price?: string;
  trailing_delta?: string;
  quantity: string;
  time_in_force?: TimeInForce;
  client_order_id?: string;
  post_only?: boolean;
  iceberg_visible_quantity?: string;
  iceberg_hidden_quantity?: string;
  sl?: SLTPConfig;
  tp?: SLTPConfig;
}

export interface OrderModifyRequest {
  price: string;
  quantity: string;
  time_in_force?: TimeInForce;
}

export interface OrderBulkCreateRequest {
  bulk_id?: string;
  orders: OrderCreateRequest[];
}

export interface OrderBulkCancelRequest {
  order_ids?: number[];
  symbol?: string;
  cancel_all?: boolean;
}
