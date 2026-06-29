import { useMemo } from "react";
import { useOrderbookStore } from "@/store/orderbookStore";
import { OrderBookRow } from "./OrderBookRow";
import { Spread } from "./Spread";

const DEPTH = 15;

export function OrderBookPanel() {
  const bids = useOrderbookStore((s) => s.bids);
  const asks = useOrderbookStore((s) => s.asks);
  const spread = useOrderbookStore((s) => s.spread);

  const topBids = bids.slice(0, DEPTH);
  const topAsks = asks.slice(0, DEPTH);
  const maxVolume = useMemo(
    () => Math.max(
      ...topBids.map((l) => l.volume),
      ...topAsks.map((l) => l.volume),
      1,
    ),
    [topBids, topAsks],
  );

  const bidPrice = topBids[0]?.price ?? null;
  const askPrice = topAsks[0]?.price ?? null;

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-3 py-2">
        <h2 className="text-sm font-semibold text-gray-100">Order Book</h2>
      </div>
      <div className="flex items-center justify-between px-2 py-1 text-xs text-gray-500">
        <span>Price (USDT)</span>
        <span>Volume (BTC)</span>
      </div>
      {/* Asks (reversed so best ask is at bottom, near spread) */}
      <div className="flex-1 overflow-y-auto">
        {[...topAsks].reverse().map((level) => (
          <OrderBookRow
            key={`ask-${level.price}`}
            price={level.price}
            volume={level.volume}
            side="ask"
            maxVolume={maxVolume}
          />
        ))}
      </div>
      <Spread spread={spread} bidPrice={bidPrice} askPrice={askPrice} />
      {/* Bids */}
      <div className="flex-1 overflow-y-auto">
        {topBids.map((level) => (
          <OrderBookRow
            key={`bid-${level.price}`}
            price={level.price}
            volume={level.volume}
            side="bid"
            maxVolume={maxVolume}
          />
        ))}
      </div>
    </div>
  );
}
