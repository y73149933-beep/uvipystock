import { api } from "./client";
import type { Order, OrderCreateRequest, OrderModifyRequest, OrderBulkCreateRequest, OrderBulkCancelRequest } from "@/types/order";

interface OrderListResponse {
  orders: Order[];
  pagination: { offset: number; limit: number; count: number };
}

interface OrderBulkCreateResponse {
  bulk_id: string;
  result: "success" | "rejected";
  total: number;
  succeeded: number;
  orders: Order[];
  errors: { code: string; message: string }[];
}

interface OrderBulkCancelResponse {
  canceled_count: number;
  canceled_orders: number[];
  failed: { index: number; code: string; message: string }[];
  total_unlocked: { asset: string; amount: string }[];
}

export const ordersApi = {
  create: (body: OrderCreateRequest) =>
    api.post<Order>("/api/v1/orders", body),

  bulkCreate: (body: OrderBulkCreateRequest) =>
    api.post<OrderBulkCreateResponse>("/api/v1/orders/bulk", body),

  modify: (orderId: number, body: OrderModifyRequest) =>
    api.put<Order>(`/api/v1/orders/${orderId}`, body),

  cancel: (orderId: number) =>
    api.delete<{ order_id: number; status: string }>(`/api/v1/orders/${orderId}`),

  bulkCancel: (body: OrderBulkCancelRequest) =>
    api.delete<OrderBulkCancelResponse>("/api/v1/orders/bulk", body),

  list: (params?: { symbol?: string; status?: string; offset?: number; limit?: number }) =>
    api.get<OrderListResponse>("/api/v1/orders", params),
};
