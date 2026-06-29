import { useMemo } from "react";
import { useBalanceStore } from "@/store/balanceStore";
import { useAuthStore } from "@/store/authStore";
import { useOrderbookStore } from "@/store/orderbookStore";
import { formatUSD, formatPrice } from "@/lib/format";
import { Button } from "@/components/common/Button";

const SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"];

export function Header() {
  const balances = useBalanceStore((s) => s.balances);
  const logout = useAuthStore((s) => s.logout);
  const symbol = useOrderbookStore((s) => s.symbol);
  const setSymbol = useOrderbookStore((s) => s.setSymbol);
  const lastTradePrice = useOrderbookStore((s) => s.lastTradePrice);

  const totalUSDT = useMemo(() => {
    // Sum all balances converted to USDT (simplified: just sum USDT + BTC*price)
    let total = 0;
    for (const b of Object.values(balances)) {
      if (b.asset === "USDT") {
        total += parseFloat(b.total);
      } else if (b.asset === "BTC" && lastTradePrice) {
        total += parseFloat(b.total) * lastTradePrice;
      }
    }
    return total;
  }, [balances, lastTradePrice]);

  const availableUSDT = useMemo(() => {
    let total = 0;
    for (const b of Object.values(balances)) {
      if (b.asset === "USDT") {
        total += parseFloat(b.available);
      } else if (b.asset === "BTC" && lastTradePrice) {
        total += parseFloat(b.available) * lastTradePrice;
      }
    }
    return total;
  }, [balances, lastTradePrice]);

  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-panel px-4">
      <div className="flex items-center gap-6">
        <h1 className="text-lg font-bold text-accent">₿ Exchange Sandbox</h1>
        <select
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="rounded border border-border bg-panelLight px-3 py-1 text-sm text-gray-100 focus:border-accent focus:outline-none"
        >
          {SYMBOLS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
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
          <div className="font-mono text-sm text-gray-100">{formatUSD(totalUSDT)} USDT</div>
        </div>
        <div className="text-right">
          <div className="text-xs text-gray-500">Available</div>
          <div className="font-mono text-sm text-bid">{formatUSD(availableUSDT)} USDT</div>
        </div>
        <Button variant="ghost" size="sm" onClick={logout}>
          Logout
        </Button>
      </div>
    </header>
  );
}
