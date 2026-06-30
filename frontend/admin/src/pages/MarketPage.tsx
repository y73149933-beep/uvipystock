import { AdminLayout } from "@/components/layout/AdminLayout";
import { TradingPairList } from "@/components/market/TradingPairList";

export function MarketPage() {
  return (
    <AdminLayout>
      <TradingPairList />
    </AdminLayout>
  );
}
