import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { marketApi } from "@/api/market";
import type { AdminTradingPairCreateRequest } from "@/types/admin";

interface TradingPairFormProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

const DEFAULTS: AdminTradingPairCreateRequest = {
  symbol: "",
  base_asset: "",
  quote_asset: "USDT",
  price_precision: 2,
  quantity_precision: 8,
  min_lot_size: "0.0001",
  max_lot_size: "1000",
  tick_size: "0.01",
  maker_fee_bps: "0",
  taker_fee_bps: "0",
};

export function TradingPairForm({ open, onClose, onCreated }: TradingPairFormProps) {
  const [form, setForm] = useState<AdminTradingPairCreateRequest>(DEFAULTS);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const set = (field: keyof AdminTradingPairCreateRequest, value: string | number) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.symbol || !form.base_asset) {
      setError("Symbol and base_asset are required");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await marketApi.createPair(form);
      onCreated();
      onClose();
      setForm(DEFAULTS);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-border bg-panel p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="mb-4 text-lg font-semibold">Create Trading Pair (Market)</h3>
        <p className="mb-4 text-sm text-gray-400">
          Create a new trading pair from any two assets. The symbol must be
          in BASE/QUOTE format (e.g. BTC/USD, ORION/RUR, ETH/USD).
          Assets are created automatically — just type the ticker.
        </p>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Symbol (e.g. BTC/USD)"
              value={form.symbol}
              onChange={(e) => set("symbol", e.target.value)}
              placeholder="BTC/USD"
              required
            />
            <Input
              label="Base Asset"
              value={form.base_asset}
              onChange={(e) => set("base_asset", e.target.value)}
              placeholder="BTC"
              required
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Quote Asset"
              value={form.quote_asset}
              onChange={(e) => set("quote_asset", e.target.value)}
              placeholder="USD"
            />
            <Input
              label="Tick Size"
              value={form.tick_size}
              onChange={(e) => set("tick_size", e.target.value)}
              placeholder="0.01"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Price Precision"
              type="number"
              value={form.price_precision}
              onChange={(e) => set("price_precision", parseInt(e.target.value))}
            />
            <Input
              label="Quantity Precision"
              type="number"
              value={form.quantity_precision}
              onChange={(e) => set("quantity_precision", parseInt(e.target.value))}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Min Lot Size"
              value={form.min_lot_size}
              onChange={(e) => set("min_lot_size", e.target.value)}
            />
            <Input
              label="Max Lot Size"
              value={form.max_lot_size}
              onChange={(e) => set("max_lot_size", e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Maker Fee (bps)"
              value={form.maker_fee_bps}
              onChange={(e) => set("maker_fee_bps", e.target.value)}
            />
            <Input
              label="Taker Fee (bps)"
              value={form.taker_fee_bps}
              onChange={(e) => set("taker_fee_bps", e.target.value)}
            />
          </div>
          {error && <p className="text-sm text-ask">{error}</p>}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={loading}>
              Create
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
