import { useEffect } from "react";
import { TradingLayout } from "@/components/layout/TradingLayout";
import { OrderBookPanel } from "@/components/orderbook/OrderBookPanel";
import { CandleChart } from "@/components/chart/CandleChart";
import { TradeForm } from "@/components/trade-form/TradeForm";
import { OpenOrdersTable } from "@/components/orders-table/OpenOrdersTable";
import { useOrderbookFeed } from "@/hooks/useOrderbook";
import { usePrivateFeed } from "@/hooks/usePrivateFeed";
import { useAuthStore } from "@/store/authStore";
import { useBalanceStore } from "@/store/balanceStore";
import { useOrdersStore } from "@/store/ordersStore";
import { useOrderbookStore } from "@/store/orderbookStore";

export function TradingPage() {
  const symbol = useOrderbookStore((s) => s.symbol);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const fetchBalances = useBalanceStore((s) => s.fetchBalances);
  const fetchOpenOrders = useOrdersStore((s) => s.fetchOpenOrders);

  // Public WS feed for orderbook + trades
  useOrderbookFeed(symbol);

  // Private WS feed for order/balance updates (only when authenticated)
  usePrivateFeed();

  // Fetch initial data on mount
  useEffect(() => {
    if (isAuthenticated) {
      fetchBalances();
      fetchOpenOrders();
    }
  }, [isAuthenticated, fetchBalances, fetchOpenOrders]);

  // Refresh open orders periodically (fallback if WS misses events)
  useEffect(() => {
    if (!isAuthenticated) return;
    const interval = setInterval(() => fetchOpenOrders(), 10000);
    return () => clearInterval(interval);
  }, [isAuthenticated, fetchOpenOrders]);

  return (
    <TradingLayout
      leftPanel={<OrderBookPanel />}
      center={<CandleChart />}
      rightPanel={<TradeForm />}
      bottom={<OpenOrdersTable />}
    />
  );
}
