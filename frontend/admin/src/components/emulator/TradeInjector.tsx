import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { emulatorApi } from "@/api/emulator";
import type { AdminTradingPair } from "@/types/admin";
import { cn } from "@/lib/utils";

interface TradeInjectorProps {
  pairs: AdminTradingPair[];
}

export function TradeInjector({ pairs }: TradeInjectorProps) {
  const [symbol, setSymbol] = useState(pairs[0]?.symbol || "BTC/USDT");
  const [price, setPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [loading, setLoading] = useState(false);
  const [lastResult, setLastResult] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!price || !quantity) return;
    setLoading(true);
    try {
      const resp = await emulatorApi.injectTrade({
        symbol,
        price,
        quantity,
        side,
      });
      setLastResult(`${resp.status} — ${side} ${quantity} @ ${price}`);
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="rounded border border-border bg-panel p-4">
      <h3 className="mb-3 font-semibold">💉 Trade Injector</h3>
      <p className="mb-4 text-sm text-gray-400">
        Inject a single synthetic trade print. Useful for testing stop-order
        triggers and chart candle formation.
      </p>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-400">Symbol</label>
          <select
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="w-full rounded border border-border bg-panel px-3 py-2 text-sm text-gray-100"
          >
            {pairs.map((p) => (
              <option key={p.symbol} value={p.symbol}>
                {p.symbol}
              </option>
            ))}
          </select>
        </div>
        <div className="grid grid-cols-2 gap-1 rounded border border-border bg-panel p-1">
          <button
            type="button"
            onClick={() => setSide("buy")}
            className={cn(
              "rounded py-1.5 text-sm font-semibold transition-colors",
              side === "buy" ? "bg-bid text-white" : "text-gray-400",
            )}
          >
            Buy
          </button>
          <button
            type="button"
            onClick={() => setSide("sell")}
            className={cn(
              "rounded py-1.5 text-sm font-semibold transition-colors",
              side === "sell" ? "bg-ask text-white" : "text-gray-400",
            )}
          >
            Sell
          </button>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Input
            label="Price"
            type="number"
            step="0.01"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            placeholder="42150.00"
          />
          <Input
            label="Quantity"
            type="number"
            step="0.0001"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            placeholder="0.123"
          />
        </div>
        {lastResult && (
          <div className="rounded border border-accent bg-accent/10 p-2 text-sm text-accent">
            ✓ {lastResult}
          </div>
        )}
        <Button type="submit" loading={loading} className="w-full">
          Inject Trade
        </Button>
      </form>
    </div>
  );
}
