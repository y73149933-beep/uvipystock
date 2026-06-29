import { useEffect, useState } from "react";
import { balancesApi } from "@/api/balances";
import type { AdminBalance } from "@/types/admin";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { BalanceAdjustForm } from "./BalanceAdjustForm";
import { formatUSD } from "@/lib/utils";

export function BalanceList() {
  const [balances, setBalances] = useState<AdminBalance[]>([]);
  const [loading, setLoading] = useState(true);
  const [userId, setUserId] = useState("");
  const [showAdjust, setShowAdjust] = useState(false);

  const fetchBalances = async (uid: number) => {
    setLoading(true);
    try {
      const resp = await balancesApi.getUserBalances(uid);
      setBalances(resp);
    } catch (err) {
      alert((err as Error).message);
      setBalances([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (userId) {
      fetchBalances(parseInt(userId));
    }
  }, [userId]);

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xl font-semibold">Balances</h2>
        <Button onClick={() => setShowAdjust(true)}>+ Adjust Balance</Button>
      </div>

      <div className="mb-4 flex gap-2">
        <Input
          type="number"
          placeholder="User ID"
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          className="w-48"
        />
        {userId && (
          <Button variant="secondary" onClick={() => fetchBalances(parseInt(userId))}>
            Refresh
          </Button>
        )}
      </div>

      {loading ? (
        <div className="py-8 text-center text-gray-500">Loading...</div>
      ) : balances.length === 0 && userId ? (
        <div className="py-8 text-center text-gray-500">No balances for this user</div>
      ) : (
        <div className="overflow-hidden rounded border border-border">
          <table className="w-full text-sm">
            <thead className="bg-panel">
              <tr className="text-left text-gray-400">
                <th className="px-4 py-2">Asset</th>
                <th className="px-4 py-2 text-right">Total</th>
                <th className="px-4 py-2 text-right">Locked</th>
                <th className="px-4 py-2 text-right">Available</th>
              </tr>
            </thead>
            <tbody>
              {balances.map((b) => (
                <tr key={`${b.user_id}-${b.asset}`} className="border-t border-border">
                  <td className="px-4 py-2 font-mono font-semibold">{b.asset}</td>
                  <td className="px-4 py-2 text-right font-mono">{formatUSD(b.total)}</td>
                  <td className="px-4 py-2 text-right font-mono text-ask">{formatUSD(b.locked)}</td>
                  <td className="px-4 py-2 text-right font-mono text-bid">{formatUSD(b.available)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <BalanceAdjustForm open={showAdjust} onClose={() => setShowAdjust(false)} onAdjusted={() => {
        if (userId) fetchBalances(parseInt(userId));
      }} />
    </div>
  );
}
