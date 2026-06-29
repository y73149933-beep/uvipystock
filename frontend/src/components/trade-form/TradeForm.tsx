import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { OrderTypeSelect } from "./OrderTypeSelect";
import { PriceInput } from "./PriceInput";
import { QuantityInput } from "./QuantityInput";
import { SLTPInputs } from "./SlTpInputs";
import { useToast } from "@/components/common/Toast";
import { useOrderbookStore } from "@/store/orderbookStore";
import { useBalanceStore } from "@/store/balanceStore";
import { ordersApi } from "@/api/orders";
import type { OrderSide, OrderType, SLTPConfig } from "@/types/order";
import { cn } from "@/lib/utils";

export function TradeForm() {
  const { showToast } = useToast();
  const symbol = useOrderbookStore((s) => s.symbol);
  const bestBid = useOrderbookStore((s) => s.bids[0]?.price ?? null);
  const bestAsk = useOrderbookStore((s) => s.asks[0]?.price ?? null);

  const [side, setSide] = useState<OrderSide>("buy");
  const [orderType, setOrderType] = useState<OrderType>("limit");
  const [price, setPrice] = useState("");
  const [stopPrice, setStopPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [icebergVisible, setIcebergVisible] = useState("");
  const [trailingDelta, setTrailingDelta] = useState("");

  // SL/TP
  const [slEnabled, setSlEnabled] = useState(false);
  const [tpEnabled, setTpEnabled] = useState(false);
  const [slStopPrice, setSlStopPrice] = useState("");
  const [slLimitPrice, setSlLimitPrice] = useState("");
  const [tpLimitPrice, setTpLimitPrice] = useState("");

  const [loading, setLoading] = useState(false);

  const baseAsset = symbol.split("/")[0] || "BTC";
  const quoteAsset = symbol.split("/")[1] || "USDT";

  const availableBase = useBalanceStore((s) => s.getAvailable(baseAsset));
  const availableQuote = useBalanceStore((s) => s.getAvailable(quoteAsset));

  const needsPrice = ["limit", "post_only", "ioc", "fok", "iceberg", "stop_limit"].includes(orderType);
  const needsStopPrice = ["stop_market", "stop_limit"].includes(orderType);
  const needsTrailing = orderType === "trailing_stop";
  const needsIceberg = orderType === "iceberg";

  const total = (() => {
    const p = parseFloat(price) || 0;
    const q = parseFloat(quantity) || 0;
    return p * q;
  })();

  const handleSubmit = async () => {
    if (!quantity || parseFloat(quantity) <= 0) {
      showToast("Quantity must be positive", "error");
      return;
    }
    if (needsPrice && (!price || parseFloat(price) <= 0)) {
      showToast("Price must be positive", "error");
      return;
    }

    setLoading(true);
    try {
      const sl: SLTPConfig | undefined = slEnabled
        ? {
            type: slLimitPrice ? "stop_limit" : "stop_market",
            stop_price: slStopPrice,
            price: slLimitPrice || undefined,
          }
        : undefined;

      const tp: SLTPConfig | undefined = tpEnabled
        ? {
            type: "limit",
            price: tpLimitPrice,
          }
        : undefined;

      const req = {
        symbol,
        side,
        type: orderType,
        price: needsPrice ? price : undefined,
        stop_price: needsStopPrice ? stopPrice : undefined,
        trailing_delta: needsTrailing ? trailingDelta : undefined,
        quantity,
        iceberg_visible_quantity: needsIceberg ? icebergVisible : undefined,
        iceberg_hidden_quantity: needsIceberg
          ? String(Math.max(0, parseFloat(quantity) - parseFloat(icebergVisible || "0")))
          : undefined,
        sl,
        tp,
      };

      const order = await ordersApi.create(req);
      showToast(`Order #${order.id} placed`, "success");

      // Reset form
      setPrice("");
      setStopPrice("");
      setQuantity("");
      setIcebergVisible("");
      setTrailingDelta("");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to place order";
      showToast(message, "error");
    } finally {
      setLoading(false);
    }
  };

  const useMaxAvailable = () => {
    if (side === "sell") {
      setQuantity(availableBase);
    } else if (bestAsk && needsPrice) {
      setPrice(String(bestAsk));
      const maxQty = parseFloat(availableQuote) / bestAsk;
      setQuantity(String(maxQty));
    }
  };

  return (
    <div className="flex h-full flex-col overflow-y-auto p-3">
      <h2 className="mb-3 text-sm font-semibold text-gray-100">Place Order</h2>

      {/* Side toggle */}
      <div className="mb-3 grid grid-cols-2 gap-1 rounded border border-border bg-panel p-1">
        <button
          onClick={() => setSide("buy")}
          className={cn(
            "rounded py-1.5 text-sm font-semibold transition-colors",
            side === "buy" ? "bg-bid text-white" : "text-gray-400 hover:text-gray-200",
          )}
        >
          Buy
        </button>
        <button
          onClick={() => setSide("sell")}
          className={cn(
            "rounded py-1.5 text-sm font-semibold transition-colors",
            side === "sell" ? "bg-ask text-white" : "text-gray-400 hover:text-gray-200",
          )}
        >
          Sell
        </button>
      </div>

      {/* Order type */}
      <div className="mb-3">
        <OrderTypeSelect value={orderType} onChange={setOrderType} />
      </div>

      {/* Price */}
      {needsPrice && (
        <div className="mb-3">
          <PriceInput
            value={price}
            onChange={setPrice}
            quoteAsset={quoteAsset}
          />
          <div className="mt-1 flex gap-1 text-xs">
            {bestBid && (
              <button
                onClick={() => setPrice(String(bestBid))}
                className="text-bid hover:underline"
              >
                Bid: {bestBid.toFixed(2)}
              </button>
            )}
            {bestAsk && (
              <button
                onClick={() => setPrice(String(bestAsk))}
                className="ml-auto text-ask hover:underline"
              >
                Ask: {bestAsk.toFixed(2)}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Stop price */}
      {needsStopPrice && (
        <div className="mb-3">
          <Input
            label="Stop Price"
            type="number"
            step="0.01"
            value={stopPrice}
            onChange={(e) => setStopPrice(e.target.value)}
            placeholder="0.00"
            suffix={quoteAsset}
          />
        </div>
      )}

      {/* Trailing delta */}
      {needsTrailing && (
        <div className="mb-3">
          <Input
            label="Trailing Delta"
            type="number"
            step="0.01"
            value={trailingDelta}
            onChange={(e) => setTrailingDelta(e.target.value)}
            placeholder="e.g. 500 (absolute)"
            suffix={quoteAsset}
          />
        </div>
      )}

      {/* Quantity */}
      <div className="mb-3">
        <QuantityInput
          value={quantity}
          onChange={setQuantity}
          baseAsset={baseAsset}
          available={side === "sell" ? availableBase : undefined}
          onUseMax={useMaxAvailable}
        />
      </div>

      {/* Iceberg visible */}
      {needsIceberg && (
        <div className="mb-3">
          <Input
            label="Visible Quantity"
            type="number"
            step="0.0001"
            value={icebergVisible}
            onChange={(e) => setIcebergVisible(e.target.value)}
            placeholder="0.0000"
            suffix={baseAsset}
          />
        </div>
      )}

      {/* Total */}
      {needsPrice && (
        <div className="mb-3 flex items-center justify-between rounded border border-border bg-panel px-3 py-2 text-sm">
          <span className="text-gray-500">Total</span>
          <span className="font-mono text-gray-100">
            {total.toFixed(2)} {quoteAsset}
          </span>
        </div>
      )}

      {/* SL/TP */}
      {(orderType === "limit" || orderType === "market") && (
        <div className="mb-3">
          <SLTPInputs
            slEnabled={slEnabled}
            tpEnabled={tpEnabled}
            slStopPrice={slStopPrice}
            slLimitPrice={slLimitPrice}
            tpLimitPrice={tpLimitPrice}
            onToggleSL={setSlEnabled}
            onToggleTP={setTpEnabled}
            onSLStopPriceChange={setSlStopPrice}
            onSLLimitPriceChange={setSlLimitPrice}
            onTPLimitPriceChange={setTpLimitPrice}
          />
        </div>
      )}

      {/* Submit */}
      <Button
        variant={side === "buy" ? "success" : "danger"}
        size="lg"
        loading={loading}
        onClick={handleSubmit}
        className="mt-auto w-full"
      >
        {side === "buy" ? "Buy" : "Sell"} {baseAsset}
      </Button>
    </div>
  );
}
