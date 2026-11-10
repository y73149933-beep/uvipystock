import { useEffect } from "react";
import { useTradeHistoryStore } from "@/store/tradeHistoryStore";
import { formatPrice, formatQty, formatTime } from "@/lib/format";
import type { Trade } from "@/types/trade";

/** The trade row's `side` is the TAKER side; what THIS user did depends
 *  on their role in the fill. */
function effectiveSide(t: Trade): "buy" | "sell" {
  const takerSide = t.side === "buy" ? "buy" : "sell";
  return t.role === "taker" ? takerSide : takerSide === "buy" ? "sell" : "buy";
}

export function MyTradesTable() {
  const trades = useTradeHistoryStore((s) => s.trades);
  const loading = useTradeHistoryStore((s) => s.loading);
  const loaded = useTradeHistoryStore((s) => s.loaded);
  const hasMore = useTradeHistoryStore((s) => s.hasMore);
  const refresh = useTradeHistoryStore((s) => s.refresh);
  const loadMore = useTradeHistoryStore((s) => s.loadMore);

  useEffect(() => {
    if (!loaded) void refresh();
  }, [loaded, refresh]);

  if (trades.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-gray-600">
        {loading ? "Loading…" : "No trades yet"}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-panelLight text-gray-500">
            <tr className="border-b border-border">
              <th className="px-2 py-1 text-left font-normal">Time</th>
              <th className="px-2 py-1 text-left font-normal">Symbol</th>
              <th className="px-2 py-1 text-left font-normal">Side</th>
              <th className="px-2 py-1 text-right font-normal">Price</th>
              <th className="px-2 py-1 text-right font-normal">Quantity</th>
              <th className="px-2 py-1 text-right font-normal">Total</th>
              <th className="px-2 py-1 text-right font-normal">Role</th>
              <th className="px-2 py-1 text-right font-normal">Fee</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => {
              const side = effectiveSide(t);
              return (
                <tr key={t.id} className="border-b border-border/40 hover:bg-panel">
                  <td className="px-2 py-1 font-mono text-gray-500">
                    {formatTime(new Date(t.executed_at).getTime())}
                  </td>
                  <td className="px-2 py-1 text-gray-200">{t.symbol}</td>
                  <td className={`px-2 py-1 ${side === "buy" ? "text-bid" : "text-ask"}`}>
                    {side === "buy" ? "Buy" : "Sell"}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-gray-300">
                    {formatPrice(t.price, 2)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-gray-300">
                    {formatQty(t.quantity, 6)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-gray-300">
                    {formatPrice(t.quote_quantity, 2)}
                  </td>
                  <td className="px-2 py-1 text-right text-[10px] uppercase text-gray-500">
                    {t.role}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-gray-500">
                    {formatPrice(t.fee, 4)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="flex shrink-0 items-center justify-center gap-3 border-t border-border py-1">
        {hasMore ? (
          <button
            onClick={() => void loadMore()}
            disabled={loading}
            className="text-[10px] font-semibold uppercase tracking-wider text-accent hover:text-gray-100 disabled:text-gray-600"
          >
            {loading ? "Loading…" : "Load older"}
          </button>
        ) : (
          <span className="text-[10px] uppercase tracking-wider text-gray-600">
            {trades.length} trades
          </span>
        )}
      </div>
    </div>
  );
}
