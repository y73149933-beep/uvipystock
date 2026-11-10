import { useState } from "react";
import { OpenOrdersTable } from "./OpenOrdersTable";
import { PositionsTable } from "./PositionsTable";

type FooterTab = "orders" | "positions";

/** Footer pane switcher: open orders vs positions/PnL. */
export function FooterTabs() {
  const [tab, setTab] = useState<FooterTab>("orders");

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 border-b border-border">
        <button
          onClick={() => setTab("orders")}
          className={`px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition-colors ${
            tab === "orders"
              ? "border-b-2 border-accent text-gray-100"
              : "text-gray-500 hover:text-gray-300"
          }`}
        >
          Open Orders
        </button>
        <button
          onClick={() => setTab("positions")}
          className={`px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition-colors ${
            tab === "positions"
              ? "border-b-2 border-accent text-gray-100"
              : "text-gray-500 hover:text-gray-300"
          }`}
        >
          Positions &amp; PnL
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {tab === "orders" ? <OpenOrdersTable /> : <PositionsTable />}
      </div>
    </div>
  );
}
