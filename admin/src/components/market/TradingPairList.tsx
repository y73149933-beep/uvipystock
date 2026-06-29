import { useEffect, useState } from "react";
import { marketApi } from "@/api/market";
import type { AdminTradingPair } from "@/types/admin";
import { Button } from "@/components/common/Button";
import { TradingPairForm } from "./TradingPairForm";
import { cn } from "@/lib/utils";

export function TradingPairList() {
  const [pairs, setPairs] = useState<AdminTradingPair[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const fetchPairs = async () => {
    setLoading(true);
    try {
      const resp = await marketApi.listPairs();
      setPairs(resp);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPairs();
  }, []);

  const handleToggle = async (pair: AdminTradingPair) => {
    try {
      await marketApi.togglePairActive(pair.id, !pair.is_active);
      await fetchPairs();
    } catch (err) {
      alert((err as Error).message);
    }
  };

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xl font-semibold">Trading Pairs ({pairs.length})</h2>
        <Button onClick={() => setShowForm(true)}>+ Create Pair</Button>
      </div>

      {error && <div className="mb-4 rounded border border-ask bg-ask/10 p-3 text-sm text-ask">{error}</div>}

      <div className="overflow-hidden rounded border border-border">
        <table className="w-full text-sm">
          <thead className="bg-panel">
            <tr className="text-left text-gray-400">
              <th className="px-4 py-2">Symbol</th>
              <th className="px-4 py-2">Base / Quote</th>
              <th className="px-4 py-2 text-right">Precision</th>
              <th className="px-4 py-2 text-right">Min Lot</th>
              <th className="px-4 py-2 text-right">Tick Size</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-gray-500">
                  Loading...
                </td>
              </tr>
            ) : pairs.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-gray-500">
                  No trading pairs
                </td>
              </tr>
            ) : (
              pairs.map((pair) => (
                <tr key={pair.id} className="border-t border-border">
                  <td className="px-4 py-2 font-mono font-semibold">{pair.symbol}</td>
                  <td className="px-4 py-2 text-gray-400">
                    {pair.base_asset} / {pair.quote_asset}
                  </td>
                  <td className="px-4 py-2 text-right font-mono text-gray-400">
                    {pair.price_precision}p / {pair.quantity_precision}q
                  </td>
                  <td className="px-4 py-2 text-right font-mono">{pair.min_lot_size}</td>
                  <td className="px-4 py-2 text-right font-mono">{pair.tick_size}</td>
                  <td className="px-4 py-2">
                    <span
                      className={cn(
                        "rounded px-2 py-0.5 text-xs",
                        pair.is_active ? "bg-bid/20 text-bid" : "bg-ask/20 text-ask",
                      )}
                    >
                      {pair.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-4 py-2">
                    <Button variant="ghost" size="sm" onClick={() => handleToggle(pair)}>
                      {pair.is_active ? "Deactivate" : "Activate"}
                    </Button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <TradingPairForm open={showForm} onClose={() => setShowForm(false)} onCreated={fetchPairs} />
    </div>
  );
}
