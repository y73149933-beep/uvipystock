import type { OrderSide } from "./order";

export interface Trade {
  id: number;
  symbol: string;
  side: OrderSide;
  price: string;
  quantity: string;
  quote_quantity: string;
  role: "taker" | "maker";
  fee: string;
  order_id: number;
  executed_at: string;
}
