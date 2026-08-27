import { useEffect, useState } from "react";
import { TradingLayout } from "@/components/layout/TradingLayout";
import { OrderBookPanel } from "@/components/orderbook/OrderBookPanel";
import { TradesFeed } from "@/components/orderbook/TradesFeed";
import { StopBookPanel } from "@/components/orderbook/StopBookPanel";
import { CandleChart } from "@/components/chart/CandleChart";
import { TradeForm } from "@/components/trade-form/TradeForm";
import { OpenOrdersTable } from "@/components/orders-table/OpenOrdersTable";
import { useOrderbookFeed } from "@/hooks/useOrderbook";
import { usePrivateFeed } from "@/hooks/usePrivateFeed";
import { useAuthStore } from "@/store/authStore";
import { useBalanceStore } from "@/store/balanceStore";
import { useOrdersStore } from "@/store/ordersStore";
import { useOrderbookStore } from "@/store/orderbookStore";
import { useStopLadderStore } from "@/store/stopLadderStore";
import { useTradesStore } from "@/store/tradesStore";

type BottomTab = "trades" | "stops";

/** Left-bottom pane switcher: trades tape vs conditional-orders ladder. */
function BottomPaneTabs() {
  const [tab, setTab] = useState<BottomTab>("trades");

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 border-b border-border">
        <button
          onClick={() => setTab("trades")}
          className={`px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition-colors ${
            tab === "trades"
              ? "border-b-2 border-accent text-gray-100"
              : "text-gray-500 hover:text-gray-300"
          }`}
        >
          Trades
        </button>
        <button
          onClick={() => setTab("stops")}
          className={`px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition-colors ${
            tab === "stops"
              ? "border-b-2 border-accent text-gray-100"
              : "text-gray-500 hover:text-gray-300"
          }`}
        >
          Conditional
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {tab === "trades" ? <TradesFeed /> : <StopBookPanel />}
      </div>
    </div>
  );
}

export function TradingPage() {
  const symbol = useOrderbookStore((s) => s.symbol);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const fetchBalances = useBalanceStore((s) => s.fetchBalances);
  const fetchOpenOrders = useOrdersStore((s) => s.fetchOpenOrders);
  const fetchRecentTrades = useTradesStore((s) => s.fetchRecent);
  const fetchLadder = useStopLadderStore((s) => s.fetchLadder);
  const clearLadder = useStopLadderStore((s) => s.clear);

  useOrderbookFeed(symbol);
  usePrivateFeed();

  // Public stop ladder: wipe on symbol switch + initial fetch per symbol
  // + slow safety-net poll.
  useEffect(() => {
    clearLadder();
    fetchLadder(symbol);
  }, [symbol, fetchLadder, clearLadder]);

  useEffect(() => {
    const interval = setInterval(() => fetchLadder(symbol), 20000);
    return () => clearInterval(interval);
  }, [symbol, fetchLadder]);

  useEffect(() => {
    if (isAuthenticated) {
      fetchBalances();
      fetchOpenOrders();
      fetchRecentTrades(symbol);
    }
  }, [isAuthenticated, fetchBalances, fetchOpenOrders, fetchRecentTrades, symbol]);

  useEffect(() => {
    if (!isAuthenticated) return;
    const interval = setInterval(() => fetchOpenOrders(), 10000);
    return () => clearInterval(interval);
  }, [isAuthenticated, fetchOpenOrders]);

  return (
    <TradingLayout
      leftPanel={<OrderBookPanel />}
      leftBottomPanel={<BottomPaneTabs />}
      center={<CandleChart />}
      rightPanel={<TradeForm />}
      bottom={<OpenOrdersTable />}
    />
  );
}
