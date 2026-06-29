import { useState } from "react";
import { Modal } from "@/components/common/Modal";
import { Input } from "@/components/common/Input";
import { Button } from "@/components/common/Button";
import { useToast } from "@/components/common/Toast";
import { ordersApi } from "@/api/orders";
import type { Order } from "@/types/order";

interface EditOrderModalProps {
  order: Order | null;
  open: boolean;
  onClose: () => void;
}

export function EditOrderModal({ order, open, onClose }: EditOrderModalProps) {
  const { showToast } = useToast();
  const [price, setPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [loading, setLoading] = useState(false);

  // Reset when order changes
  if (order && open && !price && !quantity) {
    setPrice(order.price ?? "");
    setQuantity(order.remaining_quantity);
  }

  const handleSubmit = async () => {
    if (!order) return;
    if (!price || parseFloat(price) <= 0) {
      showToast("Price must be positive", "error");
      return;
    }
    if (!quantity || parseFloat(quantity) <= 0) {
      showToast("Quantity must be positive", "error");
      return;
    }

    setLoading(true);
    try {
      await ordersApi.modify(order.id, { price, quantity });
      showToast(`Order #${order.id} modified`, "success");
      onClose();
      setPrice("");
      setQuantity("");
    } catch (err) {
      showToast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    setPrice("");
    setQuantity("");
    onClose();
  };

  return (
    <Modal open={open} onClose={handleClose} title={`Modify Order #${order?.id ?? ""}`}>
      <div className="space-y-3">
        <div className="rounded border border-border bg-panel p-2 text-xs">
          <div className="flex justify-between">
            <span className="text-gray-500">Symbol:</span>
            <span className="font-mono">{order?.symbol}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Side:</span>
            <span className={order?.side === "buy" ? "text-bid" : "text-ask"}>
              {order?.side}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Type:</span>
            <span className="font-mono">{order?.type}</span>
          </div>
        </div>
        <Input
          label="New Price"
          type="number"
          step="0.01"
          value={price}
          onChange={(e) => setPrice(e.target.value)}
        />
        <Input
          label="New Quantity"
          type="number"
          step="0.0001"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
        />
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={handleClose}>
            Cancel
          </Button>
          <Button variant="primary" size="sm" loading={loading} onClick={handleSubmit}>
            Modify
          </Button>
        </div>
      </div>
    </Modal>
  );
}
