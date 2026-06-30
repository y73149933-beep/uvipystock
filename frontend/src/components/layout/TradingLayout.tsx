import { type ReactNode } from "react";
import { Header } from "./Header";

interface TradingLayoutProps {
  header?: ReactNode;
  leftPanel?: ReactNode;
  leftBottomPanel?: ReactNode;
  center?: ReactNode;
  rightPanel?: ReactNode;
  bottom?: ReactNode;
}

/**
 * Grid-based trading terminal layout:
 *   ┌───────────────────────────────────────┐
 *   │                HEADER                  │
 *   ├──────────┬──────────────┬─────────────┤
 *   │ Orderbook│    Chart     │  Trade Form  │
 *   │  (55%)   │   (flex-1)   │   (300px)    │
 *   ├──────────┤              │              │
 *   │  Trades  │              │              │
 *   │  (45%)   │              │              │
 *   ├──────────┴──────────────┴─────────────┤
 *   │           Open Orders (220px)          │
 *   └───────────────────────────────────────┘
 */
export function TradingLayout({
  header = <Header />,
  leftPanel,
  leftBottomPanel,
  center,
  rightPanel,
  bottom,
}: TradingLayoutProps) {
  return (
    <div className="flex h-screen flex-col overflow-hidden bg-panel text-gray-100">
      {header}
      <div className="flex flex-1 gap-1 overflow-hidden p-1">
        {/* Left column: Order Book (55%) + Trades Feed (45%) */}
        <div className="flex w-[280px] shrink-0 flex-col gap-1">
          <div className="h-[55%] overflow-hidden rounded border border-border bg-panelLight">
            {leftPanel}
          </div>
          <div className="h-[45%] overflow-hidden rounded border border-border bg-panelLight">
            {leftBottomPanel}
          </div>
        </div>

        {/* Center: Chart (flex-1) */}
        <main className="flex flex-1 flex-col overflow-hidden rounded border border-border bg-panelLight">
          {center}
        </main>

        {/* Right: Trade Form (300px) */}
        <aside className="w-[300px] shrink-0 overflow-hidden rounded border border-border bg-panelLight">
          {rightPanel}
        </aside>
      </div>
      <footer className="h-[220px] shrink-0 overflow-hidden rounded border border-border bg-panelLight mx-1 mb-1">
        {bottom}
      </footer>
    </div>
  );
}
