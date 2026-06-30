import { useEffect } from "react";
import { TradingLayout } from "@/components/layout/TradingLayout";
import { OrderBookPanel } from "@/components/orderbook/OrderBookPanel";
import { TradesFeed } from "@/components/orderbook/TradesFeed";
import { CandleChart } from "@/components/chart/CandleChart";
import { TradeForm } from "@/components/trade-form/TradeForm";
import { OpenOrdersTable } from "@/components/orders-table/OpenOrdersTable";
import { useOrderbookFeed } from "@/hooks/useOrderbook";
import { usePrivateFeed } from "@/hooks/usePrivateFeed";
import { useAuthStore } from "@/store/authStore";
import { useBalanceStore } from "@/store/balanceStore";
import { useOrdersStore } from "@/store/ordersStore";
import { useOrderbookStore } from "@/store/orderbookStore";
import { useTradesStore } from "@/store/tradesStore";

export function TradingPage() {
  const symbol = useOrderbookStore((s) => s.symbol);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const fetchBalances = useBalanceStore((s) => s.fetchBalances);
  const fetchOpenOrders = useOrdersStore((s) => s.fetchOpenOrders);
  const fetchRecentTrades = useTradesStore((s) => s.fetchRecent);

  useOrderbookFeed(symbol);
  usePrivateFeed();

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
      leftBottomPanel={<TradesFeed />}
      center={<CandleChart />}
      rightPanel={<TradeForm />}
      bottom={<OpenOrdersTable />}
    />
  );
}
