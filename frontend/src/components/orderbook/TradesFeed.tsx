import { useTradesStore } from "@/store/tradesStore";
import { useOrderbookStore } from "@/store/orderbookStore";
import { formatPrice, formatQty, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export function TradesFeed() {
  const trades = useTradesStore((s) => s.recentTrades);
  const symbol = useOrderbookStore((s) => s.symbol);

  const baseAsset = symbol.split("/")[0] || "BTC";
  const quoteAsset = symbol.split("/")[1] || "USD";

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-3 py-2">
        <h2 className="text-sm font-semibold text-gray-100">
          Trades ({trades.length})
        </h2>
      </div>
      <div className="flex items-center justify-between px-2 py-1 text-xs text-gray-500">
        <span>Price</span>
        <span>Amount</span>
        <span>Time</span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {trades.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-gray-600">
            No trades yet
          </div>
        ) : (
          trades.map((t, i) => (
            <div
              key={`${t.trade_id}-${i}`}
              className={cn(
                "flex items-center justify-between px-2 py-0.5 text-xs hover:bg-panel",
                t.side === "buy" ? "text-bid" : "text-ask",
              )}
            >
              <span className="font-mono">{formatPrice(t.price, 2)}</span>
              <span className="font-mono text-gray-300">{formatQty(t.quantity, 6)}</span>
              <span className="font-mono text-gray-500">{formatTime(t.ts)}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
