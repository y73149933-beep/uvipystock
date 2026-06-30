import { AdminLayout } from "@/components/layout/AdminLayout";
import { BalanceList } from "@/components/balances/BalanceList";

export function BalancesPage() {
  return (
    <AdminLayout>
      <BalanceList />
    </AdminLayout>
  );
}
