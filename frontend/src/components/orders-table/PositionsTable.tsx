import { usePositionStore } from "@/store/positionStore";
import { formatPrice, formatQty } from "@/lib/format";

function pnlClass(v: number): string {
  if (v > 0) return "text-bid";
  if (v < 0) return "text-ask";
  return "text-gray-400";
}

function fmtPnl(v: number): string {
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}`;
}

export function PositionsTable() {
  const positions = usePositionStore((s) => s.positions);
  const loading = usePositionStore((s) => s.loading);

  const live = positions.filter((p) => p.quantity !== 0 || p.realized_pnl !== 0);

  if (live.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-gray-600">
        {loading ? "Loading…" : "No positions yet — your trading will appear here"}
      </div>
    );
  }

  const totalUnrealized = live.reduce((s, p) => s + p.unrealized_pnl, 0);
  const totalRealized = live.reduce((s, p) => s + p.realized_pnl, 0);

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-panelLight text-gray-500">
            <tr className="border-b border-border">
              <th className="px-2 py-1 text-left font-normal">Symbol</th>
              <th className="px-2 py-1 text-right font-normal">Position</th>
              <th className="px-2 py-1 text-right font-normal">Avg Cost</th>
              <th className="px-2 py-1 text-right font-normal">Last</th>
              <th className="px-2 py-1 text-right font-normal">Value</th>
              <th className="px-2 py-1 text-right font-normal">Unrealized PnL</th>
              <th className="px-2 py-1 text-right font-normal">Realized PnL</th>
            </tr>
          </thead>
          <tbody>
            {live.map((p) => (
              <tr key={p.symbol} className="border-b border-border/40 hover:bg-panel">
                <td className="px-2 py-1 text-left text-gray-200">{p.symbol}</td>
                <td className="px-2 py-1 text-right font-mono text-gray-300">
                  {formatQty(p.quantity, 6)}
                  {p.locked > 0 && (
                    <span className="ml-1 text-[10px] text-gray-500">
                      ({formatQty(p.locked, 4)} locked)
                    </span>
                  )}
                </td>
                <td className="px-2 py-1 text-right font-mono text-gray-300">
                  {formatPrice(p.avg_cost, 2)}
                </td>
                <td className="px-2 py-1 text-right font-mono text-gray-300">
                  {formatPrice(p.last_price, 2)}
                </td>
                <td className="px-2 py-1 text-right font-mono text-gray-300">
                  {formatPrice(p.position_value, 2)}
                </td>
                <td className={`px-2 py-1 text-right font-mono ${pnlClass(p.unrealized_pnl)}`}>
                  {fmtPnl(p.unrealized_pnl)}
                </td>
                <td className={`px-2 py-1 text-right font-mono ${pnlClass(p.realized_pnl)}`}>
                  {fmtPnl(p.realized_pnl)}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-border text-gray-300">
              <td className="px-2 py-1 text-left font-semibold" colSpan={5}>
                Totals
              </td>
              <td className={`px-2 py-1 text-right font-mono font-semibold ${pnlClass(totalUnrealized)}`}>
                {fmtPnl(totalUnrealized)}
              </td>
              <td className={`px-2 py-1 text-right font-mono font-semibold ${pnlClass(totalRealized)}`}>
                {fmtPnl(totalRealized)}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}
