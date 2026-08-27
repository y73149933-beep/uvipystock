/**
 * Order book panel.
 *
 * Features beyond the plain depth view:
 * 1. Clicking any row autofills the price field of the trade form
 *    (via the tradeFormStore bridge signal).
 * 2. The user's OWN conditional orders (stop_market / stop_limit) are shown
 *    as yellow trigger markers on their trigger prices. Stops deliberately
 *    never mix into the depth bars: a resting stop is not executable volume,
 *    real exchanges do not add it to the book either. trailing_stop markers
 *    are omitted because the frontend does not know the live trailed price
 *    (it is recomputed server-side in Redis on every trade).
 */
import { useMemo } from "react";
import { useOrderbookStore } from "@/store/orderbookStore";
import { useOrdersStore } from "@/store/ordersStore";
import { useTradeFormStore } from "@/store/tradeFormStore";
import type { OrderType } from "@/types/order";
import { formatPrice, formatQty } from "@/lib/format";
import { cn } from "@/lib/utils";
import { OrderBookRow } from "./OrderBookRow";
import { Spread } from "./Spread";

const DEPTH = 15;
const TRIGGER_TYPES: readonly OrderType[] = ["stop_market", "stop_limit"];

interface Level {
  price: number;
  volume: number;
}

interface UserTrigger {
  id: number;
  type: string;
  side: string;
  triggerPrice: number;
  quantity: string;
}

type BookRow =
  | { kind: "level"; level: Level; hits: UserTrigger[] }
  | { kind: "trigger"; trigger: UserTrigger };

/** Relative float equality — WS prices are numbers, REST prices are strings. */
function samePrice(a: number, b: number): boolean {
  const scale = Math.max(1, Math.abs(a), Math.abs(b));
  return Math.abs(a - b) <= 1e-9 * scale;
}

function TriggerRow({ trigger }: { trigger: UserTrigger }) {
  const label = trigger.type === "stop_limit" ? "STOP LMT" : "STOP MKT";
  const arrow = trigger.side === "buy" ? "▲" : "▼";
  return (
    <div
      className="flex items-center justify-between border-l-2 border-yellow-500 bg-yellow-500/5 px-2 py-0.5 text-xs"
      title={`Your ${trigger.type} order #${trigger.id} triggers at this price`}
    >
      <span className="font-mono text-yellow-500">
        ⚡{arrow} {label} · {formatPrice(trigger.triggerPrice)}
      </span>
      <span className="font-mono text-yellow-500/70">
        {formatQty(trigger.quantity, 6)}
      </span>
    </div>
  );
}

export function OrderBookPanel() {
  const bids = useOrderbookStore((s) => s.bids);
  const asks = useOrderbookStore((s) => s.asks);
  const spread = useOrderbookStore((s) => s.spread);
  const symbol = useOrderbookStore((s) => s.symbol);
  const openOrders = useOrdersStore((s) => s.openOrders);
  const requestPriceFill = useTradeFormStore((s) => s.requestPriceFill);

  // Parse assets from symbol (e.g. "BTC/USD" → base="BTC", quote="USD")
  const baseAsset = symbol.split("/")[0] || "BTC";
  const quoteAsset = symbol.split("/")[1] || "USD";

  // The user's pending stops for THIS symbol that have a known trigger price
  const triggers = useMemo(() => {
    return openOrders.flatMap<UserTrigger>((o) => {
      if (!TRIGGER_TYPES.includes(o.type)) return [];
      if (o.stop_price === null || o.symbol !== symbol) return [];
      const p = parseFloat(o.stop_price);
      if (!Number.isFinite(p)) return [];
      return [
        {
          id: o.id,
          type: o.type as string,
          side: o.side as string,
          triggerPrice: p,
          quantity: o.remaining_quantity || o.quantity,
        },
      ];
    });
  }, [openOrders, symbol]);

  /**
   * Merge depth levels and trigger markers into a single render sequence.
   * `levels` must already be sorted by price DESCENDING (= render order for
   * both sections: asks come pre-reversed). A trigger priced exactly at an
   * existing level collapses into that level's badge instead of its own row.
   */
  const buildRows = (levels: Level[]): BookRow[] => {
    const sortedTriggers = [...triggers].sort(
      (a, b) => b.triggerPrice - a.triggerPrice,
    );
    const rows: BookRow[] = [];
    let ti = 0;
    for (const level of levels) {
      while (
        ti < sortedTriggers.length &&
        sortedTriggers[ti].triggerPrice > level.price &&
        !samePrice(sortedTriggers[ti].triggerPrice, level.price)
      ) {
        rows.push({ kind: "trigger", trigger: sortedTriggers[ti++] });
      }
      const hits: UserTrigger[] = [];
      while (
        ti < sortedTriggers.length &&
        samePrice(sortedTriggers[ti].triggerPrice, level.price)
      ) {
        hits.push(sortedTriggers[ti++]);
      }
      rows.push({ kind: "level", level, hits });
    }
    while (ti < sortedTriggers.length) {
      rows.push({ kind: "trigger", trigger: sortedTriggers[ti++] });
    }
    return rows;
  };

  const topBids = useMemo(() => bids.slice(0, DEPTH), [bids]);
  const topAsks = useMemo(() => asks.slice(0, DEPTH), [asks]);
  const bidRows = useMemo(
    () => (triggers.length > 0 ? buildRows(topBids) : topBids.map((level) => ({ kind: "level" as const, level, hits: [] }))),
    [topBids, triggers],
  );
  const askRows = useMemo(
    () =>
      triggers.length > 0
        ? buildRows([...topAsks].reverse())
        : [...topAsks]
            .reverse()
            .map((level) => ({ kind: "level" as const, level, hits: [] })),
    [topAsks, triggers],
  );

  const maxVolume = useMemo(
    () =>
      Math.max(...topBids.map((l) => l.volume), ...topAsks.map((l) => l.volume), 1),
    [topBids, topAsks],
  );

  const bidPrice = topBids[0]?.price ?? null;
  const askPrice = topAsks[0]?.price ?? null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 className="text-sm font-semibold text-gray-100">Order Book</h2>
        {triggers.length > 0 && (
          <span
            className={cn("rounded bg-yellow-500/10 px-1.5 py-0.5 text-xs text-yellow-500")}
            title="Your pending stop orders for this symbol"
          >
            ⚡ {triggers.length}
          </span>
        )}
      </div>
      <div className="flex items-center justify-between px-2 py-1 text-xs text-gray-500">
        <span>Price ({quoteAsset})</span>
        <span>Volume ({baseAsset})</span>
      </div>
      {/* Asks (rendered price-descending so best ask sits above the spread) */}
      <div className="flex-1 overflow-y-auto">
        {askRows.map((row, i) => {
          if (row.kind === "trigger") {
            return <TriggerRow key={`at-${row.trigger.id}`} trigger={row.trigger} />;
          }
          return (
            <OrderBookRow
              key={`ask-${row.level.price}-${i}`}
              price={row.level.price}
              volume={row.level.volume}
              side="ask"
              maxVolume={maxVolume}
              onClick={requestPriceFill}
              ownStopCount={row.hits.length}
            />
          );
        })}
      </div>
      <Spread spread={spread} bidPrice={bidPrice} askPrice={askPrice} />
      {/* Bids */}
      <div className="flex-1 overflow-y-auto">
        {bidRows.map((row, i) => {
          if (row.kind === "trigger") {
            return <TriggerRow key={`bt-${row.trigger.id}`} trigger={row.trigger} />;
          }
          return (
            <OrderBookRow
              key={`bid-${row.level.price}-${i}`}
              price={row.level.price}
              volume={row.level.volume}
              side="bid"
              maxVolume={maxVolume}
              onClick={requestPriceFill}
              ownStopCount={row.hits.length}
            />
          );
        })}
      </div>
    </div>
  );
}

// formatPrice/formatQty come from @/lib/format (see imports)
