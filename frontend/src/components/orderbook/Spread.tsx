import { formatPrice } from "@/lib/format";

interface SpreadProps {
  spread: number | null;
  bidPrice: number | null;
  askPrice: number | null;
}

export function Spread({ spread, bidPrice, askPrice }: SpreadProps) {
  return (
    <div className="flex items-center justify-between border-y border-border px-2 py-1 text-xs">
      <div className="flex items-center gap-2">
        <span className="text-gray-500">Spread:</span>
        <span className="font-mono text-gray-200">
          {spread !== null ? formatPrice(spread, 2) : "—"}
        </span>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-gray-500">Bid:</span>
        <span className="font-mono text-bid">{bidPrice !== null ? formatPrice(bidPrice, 2) : "—"}</span>
        <span className="text-gray-500">Ask:</span>
        <span className="font-mono text-ask">{askPrice !== null ? formatPrice(askPrice, 2) : "—"}</span>
      </div>
    </div>
  );
}
