const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;

interface ChartToolbarProps {
  timeframe: string;
  onTimeframeChange: (tf: string) => void;
  symbol: string;
}

export function ChartToolbar({ timeframe, onTimeframeChange, symbol }: ChartToolbarProps) {
  return (
    <div className="flex items-center justify-between border-b border-border px-3 py-2">
      <div className="flex items-center gap-3">
        <h2 className="text-sm font-semibold text-gray-100">{symbol}</h2>
        <span className="text-xs text-gray-500">Candlestick</span>
      </div>
      <div className="flex items-center gap-1">
        {TIMEFRAMES.map((tf) => (
          <button
            key={tf}
            onClick={() => onTimeframeChange(tf)}
            className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
              timeframe === tf
                ? "bg-accent text-white"
                : "text-gray-400 hover:bg-panel hover:text-gray-200"
            }`}
          >
            {tf}
          </button>
        ))}
      </div>
    </div>
  );
}
