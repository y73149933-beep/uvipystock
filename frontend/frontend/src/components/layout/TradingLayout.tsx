import { type ReactNode } from "react";
import { Header } from "./Header";

interface TradingLayoutProps {
  header?: ReactNode;
  leftPanel?: ReactNode;
  center?: ReactNode;
  rightPanel?: ReactNode;
  bottom?: ReactNode;
}

/**
 * Grid-based trading terminal layout:
 *   ┌───────────────────────────────────────┐
 *   │             HEADER (64px)              │
 *   ├──────────┬──────────────┬─────────────┤
 *   │ Orderbook│    Chart     │  Trade Form  │
 *   │  (300px) │   (flex-1)   │   (320px)    │
 *   │          │              │              │
 *   ├──────────┴──────────────┴─────────────┤
 *   │         Open Orders (240px)            │
 *   └───────────────────────────────────────┘
 */
export function TradingLayout({
  header = <Header />,
  leftPanel,
  center,
  rightPanel,
  bottom,
}: TradingLayoutProps) {
  return (
    <div className="flex h-screen flex-col overflow-hidden bg-panel text-gray-100">
      {header}
      <div className="flex flex-1 gap-1 overflow-hidden p-1">
        <aside className="w-[300px] shrink-0 overflow-hidden rounded border border-border bg-panelLight">
          {leftPanel}
        </aside>
        <main className="flex flex-1 flex-col overflow-hidden rounded border border-border bg-panelLight">
          {center}
        </main>
        <aside className="w-[320px] shrink-0 overflow-hidden rounded border border-border bg-panelLight">
          {rightPanel}
        </aside>
      </div>
      <footer className="h-[240px] shrink-0 overflow-hidden rounded border border-border bg-panelLight mx-1 mb-1">
        {bottom}
      </footer>
    </div>
  );
}
