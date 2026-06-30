import { cn } from "@/lib/utils";
import { formatPrice, formatQty } from "@/lib/format";

interface OrderBookRowProps {
  price: number;
  volume: number;
  side: "bid" | "ask";
  maxVolume: number;
  onClick?: (price: number) => void;
}

export function OrderBookRow({ price, volume, side, maxVolume, onClick }: OrderBookRowProps) {
  const pct = maxVolume > 0 ? (volume / maxVolume) * 100 : 0;
  const isBid = side === "bid";

  return (
    <div
      className="relative flex cursor-pointer items-center justify-between px-2 py-0.5 text-xs hover:bg-panel"
      onClick={() => onClick?.(price)}
    >
      {/* Volume bar background */}
      <div
        className={cn("absolute right-0 top-0 h-full", isBid ? "bg-bidBg" : "bg-askBg")}
        style={{ width: `${pct}%` }}
      />
      <span className={cn("relative z-10 font-mono", isBid ? "text-bid" : "text-ask")}>
        {formatPrice(price, 2)}
      </span>
      <span className="relative z-10 font-mono text-gray-300">
        {formatQty(volume, 6)}
      </span>
    </div>
  );
}
