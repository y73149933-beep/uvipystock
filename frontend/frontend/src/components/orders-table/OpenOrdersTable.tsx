import { useState } from "react";
import { useOrdersStore } from "@/store/ordersStore";
import { useToast } from "@/components/common/Toast";
import { ordersApi } from "@/api/orders";
import { BulkActionsBar } from "./BulkActionsBar";
import { EditOrderModal } from "./EditOrderModal";
import { Button } from "@/components/common/Button";
import { formatPrice, formatQty, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Order } from "@/types/order";

export function OpenOrdersTable() {
  const orders = useOrdersStore((s) => s.openOrders);
  const loading = useOrdersStore((s) => s.loading);
  const selectedIds = useOrdersStore((s) => s.selectedIds);
  const toggleSelect = useOrdersStore((s) => s.toggleSelect);
  const selectAll = useOrdersStore((s) => s.selectAll);
  const clearSelection = useOrdersStore((s) => s.clearSelection);
  const { showToast } = useToast();

  const [editOrder, setEditOrder] = useState<Order | null>(null);

  const handleCancel = async (orderId: number) => {
    try {
      await ordersApi.cancel(orderId);
      showToast(`Order #${orderId} canceled`, "success");
    } catch (err) {
      showToast((err as Error).message, "error");
    }
  };

  const allSelected = orders.length > 0 && selectedIds.size === orders.length;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 className="text-sm font-semibold text-gray-100">
          Open Orders ({orders.length})
        </h2>
        <BulkActionsBar onEditSelected={() => {
          const firstSelected = orders.find((o) => selectedIds.has(o.id));
          if (firstSelected) setEditOrder(firstSelected);
        }} />
      </div>

      <div className="flex-1 overflow-auto">
        {loading ? (
          <div className="flex h-full items-center justify-center text-gray-500">
            Loading...
          </div>
        ) : orders.length === 0 ? (
          <div className="flex h-full items-center justify-center text-gray-500">
            No open orders
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-panelLight">
              <tr className="text-left text-gray-500">
                <th className="px-2 py-1">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={() => (allSelected ? clearSelection() : selectAll())}
                    className="h-3 w-3 accent-accent"
                  />
                </th>
                <th className="px-2 py-1">Time</th>
                <th className="px-2 py-1">Symbol</th>
                <th className="px-2 py-1">Side</th>
                <th className="px-2 py-1">Type</th>
                <th className="px-2 py-1 text-right">Price</th>
                <th className="px-2 py-1 text-right">Quantity</th>
                <th className="px-2 py-1 text-right">Filled</th>
                <th className="px-2 py-1 text-right">Remaining</th>
                <th className="px-2 py-1">Status</th>
                <th className="px-2 py-1">Actions</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => (
                <tr
                  key={order.id}
                  className={cn(
                    "border-t border-border/50 hover:bg-panel",
                    selectedIds.has(order.id) && "bg-accent/5",
                  )}
                >
                  <td className="px-2 py-1">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(order.id)}
                      onChange={() => toggleSelect(order.id)}
                      className="h-3 w-3 accent-accent"
                    />
                  </td>
                  <td className="px-2 py-1 text-gray-400">
                    {formatTime(order.created_at)}
                  </td>
                  <td className="px-2 py-1 font-mono text-gray-200">
                    {order.symbol}
                  </td>
                  <td className={cn("px-2 py-1 font-medium", order.side === "buy" ? "text-bid" : "text-ask")}>
                    {order.side}
                  </td>
                  <td className="px-2 py-1 text-gray-400">{order.type}</td>
                  <td className="px-2 py-1 text-right font-mono text-gray-200">
                    {formatPrice(order.price, 2)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-gray-200">
                    {formatQty(order.quantity, 6)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-gray-400">
                    {formatQty(order.filled_quantity, 6)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-gray-200">
                    {formatQty(order.remaining_quantity, 6)}
                  </td>
                  <td className="px-2 py-1">
                    <span
                      className={cn(
                        "rounded px-1.5 py-0.5 text-xs",
                        order.status === "new" && "bg-accent/10 text-accent",
                        order.status === "partially_filled" && "bg-yellow-500/10 text-yellow-500",
                        order.status === "pending" && "bg-purple-500/10 text-purple-400",
                      )}
                    >
                      {order.status}
                    </span>
                  </td>
                  <td className="px-2 py-1">
                    <div className="flex gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setEditOrder(order)}
                        disabled={!["limit", "post_only", "iceberg"].includes(order.type)}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleCancel(order.id)}
                        className="text-ask hover:text-ask"
                      >
                        ✕
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <EditOrderModal
        order={editOrder}
        open={editOrder !== null}
        onClose={() => setEditOrder(null)}
      />
    </div>
  );
}
