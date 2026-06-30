import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { balancesApi } from "@/api/balances";

interface BalanceAdjustFormProps {
  open: boolean;
  onClose: () => void;
  onAdjusted: () => void;
}

export function BalanceAdjustForm({ open, onClose, onAdjusted }: BalanceAdjustFormProps) {
  const [userId, setUserId] = useState("");
  const [asset, setAsset] = useState("USDT");
  const [delta, setDelta] = useState("");
  const [reason, setReason] = useState("admin_adjustment");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!userId || !asset || !delta) {
      setError("User ID, asset, and delta are required");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await balancesApi.adjust({
        user_id: parseInt(userId),
        asset,
        delta,
        reason,
      });
      onAdjusted();
      onClose();
      setDelta("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-lg border border-border bg-panel p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="mb-2 text-lg font-semibold">Adjust Balance</h3>
        <p className="mb-4 text-sm text-gray-400">
          Credit or debit any asset for a user. Type any ticker (USD, RUR,
          BTC, ETH, ORION, etc.) — the balance is created automatically.
          Use positive delta to credit, negative to debit.
        </p>
        <form onSubmit={handleSubmit} className="space-y-3">
          <Input
            label="User ID"
            type="number"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            required
          />
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-400">Asset</label>
            <input
              type="text"
              value={asset}
              onChange={(e) => setAsset(e.target.value)}
              placeholder="USD, RUR, BTC, ORION, ..."
              className="w-full rounded border border-border bg-panel px-3 py-2 text-sm text-gray-100 placeholder:text-gray-600 focus:border-accent focus:outline-none"
            />
          </div>
          <Input
            label="Delta (signed: +credit / -debit)"
            type="number"
            step="0.000001"
            value={delta}
            onChange={(e) => setDelta(e.target.value)}
            placeholder="e.g. 1000 or -500"
            required
          />
          <Input
            label="Reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          {error && <p className="text-sm text-ask">{error}</p>}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={loading}>
              Adjust
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
