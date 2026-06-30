import { useMemo, useEffect } from "react";
import { useBalanceStore } from "@/store/balanceStore";
import { useAuthStore } from "@/store/authStore";
import { useOrderbookStore } from "@/store/orderbookStore";
import { usePairsStore } from "@/store/pairsStore";
import { formatUSD, formatPrice, formatQty } from "@/lib/format";
import { Button } from "@/components/common/Button";

export function Header() {
  const balances = useBalanceStore((s) => s.balances);
  const logout = useAuthStore((s) => s.logout);
  const symbol = useOrderbookStore((s) => s.symbol);
  const setSymbol = useOrderbookStore((s) => s.setSymbol);
  const lastTradePrice = useOrderbookStore((s) => s.lastTradePrice);

  const { symbols, fetchPairs } = usePairsStore();

  useEffect(() => {
    fetchPairs();
  }, [fetchPairs]);

  // Get quote/base asset from current symbol (e.g. "BTC/USD" → "USD")
  const quoteAsset = symbol.split("/")[1] || "USD";
  const baseAsset = symbol.split("/")[0] || "BTC";

  // Total portfolio value in quote asset
  const totalValue = useMemo(() => {
    let total = 0;
    for (const b of Object.values(balances)) {
      if (b.asset === quoteAsset) {
        total += parseFloat(b.total);
      } else if (b.asset === baseAsset && lastTradePrice) {
        total += parseFloat(b.total) * lastTradePrice;
      }
    }
    return total;
  }, [balances, lastTradePrice, quoteAsset, baseAsset]);

  const availableValue = useMemo(() => {
    let total = 0;
    for (const b of Object.values(balances)) {
      if (b.asset === quoteAsset) {
        total += parseFloat(b.available);
      } else if (b.asset === baseAsset && lastTradePrice) {
        total += parseFloat(b.available) * lastTradePrice;
      }
    }
    return total;
  }, [balances, lastTradePrice, quoteAsset, baseAsset]);

  // All non-zero balances for the assets dropdown
  const allBalances = useMemo(() => {
    return Object.values(balances).filter(b => parseFloat(b.total) > 0);
  }, [balances]);

  return (
    <header className="border-b border-border bg-panel px-4">
      {/* Row 1: Logo + Symbol selector + Total/Available + Logout */}
      <div className="flex h-10 items-center justify-between">
        <div className="flex items-center gap-6">
          <h1 className="text-lg font-bold text-accent">₿ Exchange Sandbox</h1>
          <select
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="rounded border border-border bg-panelLight px-3 py-1 text-sm text-gray-100 focus:border-accent focus:outline-none"
          >
            {symbols.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          {lastTradePrice !== null && (
            <span className="font-mono text-sm text-gray-300">
              {formatPrice(lastTradePrice, 2)}
            </span>
          )}
        </div>

        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-xs text-gray-500">Total</div>
            <div className="font-mono text-sm text-gray-100">
              {formatUSD(totalValue)} {quoteAsset}
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs text-gray-500">Available</div>
            <div className="font-mono text-sm text-bid">
              {formatUSD(availableValue)} {quoteAsset}
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={logout}>Logout</Button>
        </div>
      </div>

      {/* Row 2: All asset balances (horizontal scroll) */}
      {allBalances.length > 0 && (
        <div className="flex h-8 items-center gap-4 overflow-x-auto border-t border-border/50">
          {allBalances.map((b) => (
            <div key={b.asset} className="flex shrink-0 items-center gap-1 text-xs">
              <span className="font-medium text-gray-400">{b.asset}:</span>
              <span className="font-mono text-gray-200">{formatQty(b.total, 4)}</span>
              {parseFloat(b.locked) > 0 && (
                <span className="font-mono text-ask/70">
                  ({formatQty(b.locked, 4)} locked)
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </header>
  );
}
