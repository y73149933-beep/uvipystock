import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { emulatorApi } from "@/api/emulator";
import type { AdminTradingPair } from "@/types/admin";

interface RandomWalkPanelProps {
  pairs: AdminTradingPair[];
}

export function RandomWalkPanel({ pairs }: RandomWalkPanelProps) {
  const [symbol, setSymbol] = useState(pairs[0]?.symbol || "BTC/USDT");
  const [startPrice, setStartPrice] = useState("42150");
  const [volatility, setVolatility] = useState("0.5");
  const [steps, setSteps] = useState("100");
  const [intervalMs, setIntervalMs] = useState("100");
  const [loading, setLoading] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setJobId(null);
    try {
      const resp = await emulatorApi.randomWalk({
        symbol,
        start_price: startPrice,
        volatility_pct: volatility,
        steps: parseInt(steps),
        interval_ms: parseInt(intervalMs),
      });
      setJobId(resp.job_id);
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="rounded border border-border bg-panel p-4">
      <h3 className="mb-3 font-semibold">🎲 Random Walk Emulator</h3>
      <p className="mb-4 text-sm text-gray-400">
        Generates synthetic trades with random price movement to simulate market activity.
        Useful for testing charts, stop triggers, and bot strategies.
      </p>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-400">Symbol</label>
          <select
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="w-full rounded border border-border bg-panel px-3 py-2 text-sm text-gray-100"
          >
            {pairs.map((p) => (
              <option key={p.symbol} value={p.symbol}>
                {p.symbol}
              </option>
            ))}
          </select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Input
            label="Start Price"
            type="number"
            step="0.01"
            value={startPrice}
            onChange={(e) => setStartPrice(e.target.value)}
          />
          <Input
            label="Volatility (%)"
            type="number"
            step="0.1"
            value={volatility}
            onChange={(e) => setVolatility(e.target.value)}
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Input
            label="Steps"
            type="number"
            value={steps}
            onChange={(e) => setSteps(e.target.value)}
          />
          <Input
            label="Interval (ms)"
            type="number"
            value={intervalMs}
            onChange={(e) => setIntervalMs(e.target.value)}
          />
        </div>
        {jobId && (
          <div className="rounded border border-bid bg-bid/10 p-2 text-sm text-bid">
            ✓ Started: {jobId}
          </div>
        )}
        <Button type="submit" loading={loading} className="w-full">
          Start Random Walk
        </Button>
      </form>
    </div>
  );
}
