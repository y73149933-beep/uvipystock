import { cn } from "@/lib/utils";
import { formatPrice, formatQty } from "@/lib/format";
import { useStopLadderStore } from "@/store/stopLadderStore";
import type { StopLadderLevel } from "@/api/market";

/**
 * Public conditional-order ladder ("stop book"):
 * aggregated trigger levels of ALL users' pending stop orders, anonymized.
 *
 * Layout mirrors the order book: SELL triggers on top (descending price),
 * BUY triggers below (descending price). Each row shows the type chip
 * (MKT / LMT / TRAIL), the trigger price and the aggregated volume with a
 * side-colored background bar proportional to volume.
 */

function typeLabel(type: string): string {
  if (type === "stop_market") return "MKT";
  if (type === "stop_limit") return "LMT";
  return "TRAIL";
}

function typeChipClass(type: string): string {
  switch (type) {
    case "stop_market":
      return "bg-amber-500/15 text-amber-400";
    case "stop_limit":
      return "bg-violet-500/15 text-violet-400";
    default:
      return "bg-cyan-500/15 text-cyan-400"; // trailing_stop
  }
}

function LadderRow({ level, maxVolume }: { level: StopLadderLevel; maxVolume: number }) {
  const isSell = level.side === "sell";
  const vol = Number(level.volume);
  const pct = maxVolume > 0 && Number.isFinite(vol) ? (vol / maxVolume) * 100 : 0;

  return (
    <div className="relative flex items-center gap-2 px-2 py-[3px] text-xs" title={`${level.count} order(s) at this trigger`}>
      {/* Volume bar background */}
      <div
        className={cn("absolute inset-y-0 right-0", isSell ? "bg-askBg" : "bg-bidBg")}
        style={{ width: `${pct}%` }}
      />
      <span className={cn("relative z-10 w-11 shrink-0 rounded px-1 text-center font-mono text-[9px] font-bold", typeChipClass(level.type))}>
        {typeLabel(level.type)}
      </span>
      <span className="relative z-10 w-1.5 shrink-0 font-mono text-[9px]" >
        <span className={isSell ? "text-ask" : "text-bid"}>{isSell ? "▼" : "▲"}</span>
      </span>
      <span className={cn("relative z-10 flex-1 text-right font-mono", isSell ? "text-ask" : "text-bid")}>
        {formatPrice(level.price)}
      </span>
      <span className="relative z-10 w-16 shrink-0 text-right font-mono text-gray-300">
        {formatQty(level.volume)}
      </span>
      {level.count > 1 && (
        <span className="relative z-10 w-6 shrink-0 text-right font-mono text-[9px] text-gray-500">
          ×{level.count}
        </span>
      )}
    </div>
  );
}

export function StopBookPanel() {
  const levels = useStopLadderStore((s) => s.levels);
  const totalCount = useStopLadderStore((s) => s.totalCount);
  const loading = useStopLadderStore((s) => s.loading);
  const error = useStopLadderStore((s) => s.error);

  const sells = levels.filter((l) => l.side === "sell");
  const buys = levels.filter((l) => l.side === "buy");
  const maxVolume = levels.reduce(
    (m, l) => Math.max(m, Number(l.volume) || 0),
    0,
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-2 py-1.5">
        <div className="flex items-center gap-1.5">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-200">
            Conditional Orders
          </h3>
          {totalCount > 0 && (
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[9px] font-bold text-amber-400">
              ⚡{totalCount}
            </span>
          )}
        </div>
        <span className="text-[9px] text-gray-500">anonymous</span>
      </div>

      <div className="flex items-center justify-between border-b border-border px-2 py-0.5 text-[9px] uppercase tracking-wider text-gray-500">
        <span>type</span>
        <span>trigger</span>
        <span>volume</span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto py-0.5">
        {error ? (
          <div className="px-2 py-4 text-center text-xs text-red-400">{error}</div>
        ) : levels.length === 0 ? (
          <div className="px-2 py-4 text-center text-xs text-gray-500">
            {loading ? "Loading..." : "No conditional orders"}
          </div>
        ) : (
          <>
            {/* SELL-side stops live BELOW the market.
                Sorted ASC so the trigger closest to the last price sits at
                the bottom edge of the section. */}
            {sells.length > 0 && (
              <div className="mt-0.5 mb-0.5 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ask/80">
                ▼ sell-side stops
              </div>
            )}
            {[...sells].reverse().map((l) => (
              <LadderRow key={`sell-${l.type}-${l.price}`} level={l} maxVolume={maxVolume} />
            ))}
            {/* BUY-side stops live ABOVE the market.
                Backend returns them DESC — again nearest-to-market at the bottom. */}
            {buys.length > 0 && (
              <div className="mt-1 mb-0.5 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-bid/80">
                ▲ buy-side stops
              </div>
            )}
            {buys.map((l) => (
              <LadderRow key={`buy-${l.type}-${l.price}`} level={l} maxVolume={maxVolume} />
            ))}
          </>
        )}
      </div>

      {levels.length > 0 && (
        <div className="border-t border-border px-2 py-1 text-[9px] leading-relaxed text-gray-500">
          Aggregated pending stops of all users. Sell-side triggers fire on a drop
          below their price; buy-side triggers fire on a rally above it.
        </div>
      )}
    </div>
  );
}
