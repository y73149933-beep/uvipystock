import { cn } from "@/lib/utils";
import { formatPrice, formatQty } from "@/lib/format";

interface OrderBookRowProps {
  price: number;
  volume: number;
  side: "bid" | "ask";
  maxVolume: number;
  onClick?: (price: number) => void;
  /**
   * Number of the user's OWN stop orders whose trigger price equals this
   * level. Renders a small yellow badge so resting stops stay visible
   * without polluting the depth bars.
   */
  ownStopCount?: number;
}

export function OrderBookRow({ price, volume, side, maxVolume, onClick, ownStopCount = 0 }: OrderBookRowProps) {
  const pct = maxVolume > 0 ? (volume / maxVolume) * 100 : 0;
  const isBid = side === "bid";

  return (
    <div
      className="relative flex cursor-pointer items-center justify-between px-2 py-0.5 text-xs hover:bg-panel"
      onClick={() => onClick?.(price)}
      title="Click to fill the order form price"
    >
      {/* Volume bar background */}
      <div
        className={cn("absolute right-0 top-0 h-full", isBid ? "bg-bidBg" : "bg-askBg")}
        style={{ width: `${pct}%` }}
      />
      <span className={cn("relative z-10 font-mono", isBid ? "text-bid" : "text-ask")}>
        {ownStopCount > 0 && (
          <span
            className="mr-1 rounded bg-yellow-500/15 px-1 text-yellow-500"
            title={`${ownStopCount} of your stop order(s) trigger at this price`}
          >
            ⚡{ownStopCount > 1 ? ownStopCount : ""}
          </span>
        )}
        {formatPrice(price, 2)}
      </span>
      <span className="relative z-10 font-mono text-gray-300">
        {formatQty(volume, 6)}
      </span>
    </div>
  );
}
