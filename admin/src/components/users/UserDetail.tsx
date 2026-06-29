import { useState } from "react";
import type { AdminUser } from "@/types/admin";
import { Button } from "@/components/common/Button";
import { balancesApi } from "@/api/balances";
import type { AdminBalance } from "@/types/admin";
import { formatUSD, formatTime } from "@/lib/utils";

interface UserDetailProps {
  user: AdminUser | null;
  onClose: () => void;
}

export function UserDetail({ user, onClose }: UserDetailProps) {
  const [balances, setBalances] = useState<AdminBalance[] | null>(null);
  const [loading, setLoading] = useState(false);

  if (!user) return null;

  const loadBalances = async () => {
    if (!user) return;
    setLoading(true);
    try {
      const resp = await balancesApi.getUserBalances(user.id);
      setBalances(resp);
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-lg border border-border bg-panel p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold">User #{user.id}</h3>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300">
            ✕
          </button>
        </div>

        <div className="mb-4 grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-gray-500">Email:</span> {user.email}
          </div>
          <div>
            <span className="text-gray-500">Role:</span> {user.is_admin ? "Admin" : "User"}
          </div>
          <div>
            <span className="text-gray-500">Status:</span> {user.is_active ? "Active" : "Blocked"}
          </div>
          <div>
            <span className="text-gray-500">Created:</span> {formatTime(user.created_at)}
          </div>
        </div>

        <div className="border-t border-border pt-4">
          <div className="mb-2 flex items-center justify-between">
            <h4 className="font-medium">Balances</h4>
            <Button variant="secondary" size="sm" onClick={loadBalances} loading={loading}>
              Load Balances
            </Button>
          </div>
          {balances && (
            <table className="w-full text-sm">
              <thead className="text-gray-400">
                <tr className="text-left">
                  <th className="px-2 py-1">Asset</th>
                  <th className="px-2 py-1 text-right">Total</th>
                  <th className="px-2 py-1 text-right">Locked</th>
                  <th className="px-2 py-1 text-right">Available</th>
                </tr>
              </thead>
              <tbody>
                {balances.map((b) => (
                  <tr key={b.asset} className="border-t border-border">
                    <td className="px-2 py-1 font-mono">{b.asset}</td>
                    <td className="px-2 py-1 text-right font-mono">{formatUSD(b.total)}</td>
                    <td className="px-2 py-1 text-right font-mono text-ask">{formatUSD(b.locked)}</td>
                    <td className="px-2 py-1 text-right font-mono text-bid">{formatUSD(b.available)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
