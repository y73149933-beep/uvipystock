import { useEffect, useRef, useState } from "react";
import { createChart, ColorType, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useChartStore } from "@/store/chartStore";
import { useOrderbookStore } from "@/store/orderbookStore";
import { ChartToolbar } from "./ChartToolbar";

function tfToSeconds(tf: string): number {
  const map: Record<string, number> = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400,
  };
  return map[tf] || 60;
}

export function CandleChart() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lastBarTimeRef = useRef<number>(0);

  const symbol = useChartStore((s) => s.symbol);
  const timeframe = useChartStore((s) => s.timeframe);
  const candles = useChartStore((s) => s.candles);
  const loading = useChartStore((s) => s.loading);
  const setSymbol = useChartStore((s) => s.setSymbol);
  const setTimeframe = useChartStore((s) => s.setTimeframe);
  const fetchCandles = useChartStore((s) => s.fetchCandles);
  const updateLastPrice = useChartStore((s) => s.updateLastPrice);
  const addTradeToCandle = useChartStore((s) => s.addTradeToCandle);

  const orderbookSymbol = useOrderbookStore((s) => s.symbol);
  const bestBid = useOrderbookStore((s) => s.bids[0]?.price ?? null);
  const bestAsk = useOrderbookStore((s) => s.asks[0]?.price ?? null);
  const lastTradePrice = useOrderbookStore((s) => s.lastTradePrice);

  const [hasData, setHasData] = useState(false);

  // Create chart on mount
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#16213e" },
        textColor: "#cbd5e1",
        fontFamily: "JetBrains Mono, monospace",
      },
      grid: {
        vertLines: { color: "rgba(15, 52, 96, 0.3)" },
        horzLines: { color: "rgba(15, 52, 96, 0.3)" },
      },
      timeScale: {
        borderColor: "#0f3460",
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: { borderColor: "#0f3460" },
      crosshair: { mode: 1 },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
    });

    const series = chart.addCandlestickSeries({
      upColor: "#22c55e", downColor: "#ef4444",
      borderUpColor: "#22c55e", borderDownColor: "#ef4444",
      wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    });

    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      color: "rgba(14, 165, 233, 0.3)",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartRef.current = chart;
    seriesRef.current = series;
    volumeRef.current = volume;

    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        chart.applyOptions({ width, height });
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeRef.current = null;
    };
  }, []);

  // Fetch candles when symbol or timeframe changes
  useEffect(() => {
    fetchCandles(symbol, timeframe);
  }, [symbol, timeframe, fetchCandles]);

  // Update chart from candle store (batch setData)
  useEffect(() => {
    if (!seriesRef.current || !volumeRef.current) return;

    if (candles.length === 0) {
      seriesRef.current.setData([]);
      volumeRef.current.setData([]);
      lastBarTimeRef.current = 0;
      setHasData(false);
      return;
    }

    const candleData = candles.map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    const volumeData = candles.map((c) => ({
      time: c.time as UTCTimestamp,
      value: c.volume,
      color: c.close >= c.open ? "rgba(34,197,94,0.3)" : "rgba(239,68,68,0.3)",
    }));
    seriesRef.current.setData(candleData);
    volumeRef.current.setData(volumeData);
    lastBarTimeRef.current = candles[candles.length - 1].time;
    setHasData(true);
  }, [candles]);

  // Real-time price line from orderbook (when no candles exist yet)
  useEffect(() => {
    if (!seriesRef.current) return;
    if (candles.length > 0) return; // real candle data takes priority

    let midPrice: number | null = null;
    if (bestBid !== null && bestAsk !== null) {
      midPrice = (bestBid + bestAsk) / 2;
    } else if (lastTradePrice !== null) {
      midPrice = lastTradePrice;
    } else if (bestBid !== null) {
      midPrice = bestBid;
    } else if (bestAsk !== null) {
      midPrice = bestAsk;
    }

    if (midPrice === null) return;

    const tf = tfToSeconds(timeframe);
    const now = Math.floor(Date.now() / 1000);
    const bucketTime = Math.floor(now / tf) * tf;

    if (bucketTime >= lastBarTimeRef.current) {
      try {
        seriesRef.current.update({
          time: bucketTime as UTCTimestamp,
          open: midPrice, high: midPrice, low: midPrice, close: midPrice,
        });
        lastBarTimeRef.current = bucketTime;
        setHasData(true);
      } catch {
        // ignore
      }
    }
  }, [bestBid, bestAsk, lastTradePrice, timeframe, candles.length]);

  // Sync with orderbook symbol
  useEffect(() => {
    if (orderbookSymbol !== symbol) {
      setSymbol(orderbookSymbol);
    }
  }, [orderbookSymbol, symbol, setSymbol]);

  return (
    <div className="flex h-full flex-col">
      <ChartToolbar
        symbol={symbol}
        timeframe={timeframe}
        onTimeframeChange={setTimeframe}
      />
      <div ref={containerRef} className="relative flex-1">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-gray-500">
            Loading candles...
          </div>
        )}
        {!loading && !hasData && bestBid === null && bestAsk === null && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-gray-500">
            Waiting for market data...
          </div>
        )}
        {!loading && !hasData && (bestBid !== null || bestAsk !== null) && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-gray-500">
            No trade history — showing live price
          </div>
        )}
      </div>
    </div>
  );
}
