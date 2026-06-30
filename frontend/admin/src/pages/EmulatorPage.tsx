import { useEffect, useState } from "react";
import { AdminLayout } from "@/components/layout/AdminLayout";
import { RandomWalkPanel } from "@/components/emulator/RandomWalkPanel";
import { TradeInjector } from "@/components/emulator/TradeInjector";
import { marketApi } from "@/api/market";
import type { AdminTradingPair } from "@/types/admin";

export function EmulatorPage() {
  const [pairs, setPairs] = useState<AdminTradingPair[]>([]);

  useEffect(() => {
    marketApi.listPairs().then(setPairs).catch(console.error);
  }, []);

  return (
    <AdminLayout>
      <h1 className="mb-6 text-2xl font-bold">Market Emulator</h1>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <RandomWalkPanel pairs={pairs} />
        <TradeInjector pairs={pairs} />
      </div>
    </AdminLayout>
  );
}
