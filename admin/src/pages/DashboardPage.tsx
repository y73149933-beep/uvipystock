import { AdminLayout } from "@/components/layout/AdminLayout";

export function DashboardPage() {
  return (
    <AdminLayout>
      <h1 className="mb-6 text-2xl font-bold">Dashboard</h1>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="rounded border border-border bg-panel p-4">
          <div className="text-sm text-gray-400">Total Users</div>
          <div className="mt-1 text-2xl font-bold">—</div>
          <div className="mt-2 text-xs text-gray-500">Navigate to Users page</div>
        </div>
        <div className="rounded border border-border bg-panel p-4">
          <div className="text-sm text-gray-400">Active Pairs</div>
          <div className="mt-1 text-2xl font-bold">—</div>
          <div className="mt-2 text-xs text-gray-500">Navigate to Market page</div>
        </div>
        <div className="rounded border border-border bg-panel p-4">
          <div className="text-sm text-gray-400">Open Orders</div>
          <div className="mt-1 text-2xl font-bold">—</div>
          <div className="mt-2 text-xs text-gray-500">System operational</div>
        </div>
      </div>

      <div className="mt-8">
        <h2 className="mb-4 text-lg font-semibold">Quick Actions</h2>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <a
            href="/users"
            className="rounded border border-border bg-panel p-4 text-center transition-colors hover:border-accent"
          >
            <div className="text-2xl">👥</div>
            <div className="mt-2 text-sm">Manage Users</div>
          </a>
          <a
            href="/balances"
            className="rounded border border-border bg-panel p-4 text-center transition-colors hover:border-accent"
          >
            <div className="text-2xl">💰</div>
            <div className="mt-2 text-sm">Adjust Balances</div>
          </a>
          <a
            href="/market"
            className="rounded border border-border bg-panel p-4 text-center transition-colors hover:border-accent"
          >
            <div className="text-2xl">📈</div>
            <div className="mt-2 text-sm">Trading Pairs</div>
          </a>
          <a
            href="/emulator"
            className="rounded border border-border bg-panel p-4 text-center transition-colors hover:border-accent"
          >
            <div className="text-2xl">🎲</div>
            <div className="mt-2 text-sm">Market Emulator</div>
          </a>
        </div>
      </div>
    </AdminLayout>
  );
}
