import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { createChart, ColorType, TickMarkType, type IChartApi, type ISeriesApi, type LogicalRange, type Range, type Time, type UTCTimestamp } from "lightweight-charts";
import { useChartStore } from "@/store/chartStore";
import { useOrderbookStore } from "@/store/orderbookStore";
import { ChartToolbar } from "./ChartToolbar";
import { DepthOverlay, PANE_DIVIDER_FRAC } from "./DepthOverlay";
import { ChartTradePad } from "./ChartTradePad";
import { ChartContextMenu, type ChartMenuState } from "./ChartContextMenu";
import { useAuthStore } from "@/store/authStore";

const DEPTH_PREFS_KEY = "uvity.depthOverlay";

interface DepthPrefs {
  book: boolean;
  stops: boolean;
  publicStops: boolean;
  myOrders: boolean;
}

function loadDepthPrefs(): DepthPrefs {
  try {
    const raw = localStorage.getItem(DEPTH_PREFS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<DepthPrefs>;
      return {
        book: parsed.book ?? true,
        stops: parsed.stops ?? true,
        publicStops: parsed.publicStops ?? true,
        myOrders: parsed.myOrders ?? true,
      };
    }
  } catch {
    // ignore malformed prefs
  }
  return { book: true, stops: true, publicStops: true, myOrders: true };
}

function tfToSeconds(tf: string): number {
  const map: Record<string, number> = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400,
  };
  return map[tf] || 60;
}

function fmtNum(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(2);
  const s = v.toFixed(6);
  return s.replace(/\.?0+$/, "") || "0";
}

function fmtTime(t: number, tf: string): string {
  // browser-local timezone: the tape/tables already render local, the
  // chart axis is localized too (see tickMarkFormatter below) - one
  // clock across the whole UI
  const d = new Date(t * 1000);
  const hh = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (tf === "1d") return d.toLocaleDateString(undefined, { year: "numeric", month: "2-digit", day: "2-digit" });
  if (tf === "4h" || tf === "1h") return `${d.toLocaleDateString(undefined, { month: "2-digit", day: "2-digit" })} ${hh}`;
  return hh;
}

export function CandleChart() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lastBarTimeRef = useRef<number>(0);
  const legendRef = useRef<HTMLDivElement>(null);
  const candlesByTimeRef = useRef(new Map<number, { time: number; open: number; high: number; low: number; close: number; volume: number }>());

  const symbol = useChartStore((s) => s.symbol);
  const timeframe = useChartStore((s) => s.timeframe);
  const candles = useChartStore((s) => s.candles);
  const loading = useChartStore((s) => s.loading);
  const setSymbol = useChartStore((s) => s.setSymbol);
  const setTimeframe = useChartStore((s) => s.setTimeframe);
  const fetchCandles = useChartStore((s) => s.fetchCandles);
  const updateLastPrice = useChartStore((s) => s.updateLastPrice);
  const addTradeToCandle = useChartStore((s) => s.addTradeToCandle);
  const loadOlder = useChartStore((s) => s.loadOlder);
  const hasCandlesRef = useRef(false);
  const loadingOlderRef = useRef(false);
  const needFitRef = useRef(false);
  // visible time range captured just before an older-page fetch, so the
  // viewport stays where the user was after the data is prepended
  const preserveRangeRef = useRef<Range<Time> | null>(null);

  const orderbookSymbol = useOrderbookStore((s) => s.symbol);
  const bestBid = useOrderbookStore((s) => s.bids[0]?.price ?? null);
  const bestAsk = useOrderbookStore((s) => s.asks[0]?.price ?? null);
  const lastTradePrice = useOrderbookStore((s) => s.lastTradePrice);

  const [hasData, setHasData] = useState(false);

  // one-click trading widgets (right-click menu + pinned market pad)
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const [padQty, setPadQty] = useState(() => localStorage.getItem("uvity.chartQty") || "0.1");
  const [chartMsg, setChartMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [menu, setMenu] = useState<ChartMenuState | null>(null);

  const setPadQtyPersisted = (v: string) => {
    setPadQty(v);
    localStorage.setItem("uvity.chartQty", v);
  };

  useEffect(() => {
    if (!chartMsg) return;
    const t = setTimeout(() => setChartMsg(null), 5000);
    return () => clearTimeout(t);
  }, [chartMsg]);

  // depth histogram overlay state
  const [chartApi, setChartApi] = useState<IChartApi | null>(null);
  const [seriesApi, setSeriesApi] = useState<ISeriesApi<"Candlestick"> | null>(null);
  const [depthPrefs, setDepthPrefs] = useState<DepthPrefs>(loadDepthPrefs);

  const openMenuAtCursor = (e: ReactMouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (!isAuthenticated || !containerRef.current || !seriesRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const price = seriesRef.current.coordinateToPrice(e.clientY - rect.top);
    if (price == null) return;
    setMenu({
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
      price: Number(price).toFixed(2),
    });
  };

  const toggleDepth = (key: keyof DepthPrefs) => {
    setDepthPrefs((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try {
        localStorage.setItem(DEPTH_PREFS_KEY, JSON.stringify(next));
      } catch {
        // storage unavailable — keep session-only prefs
      }
      return next;
    });
  };

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
        // lightweight-charts formats ticks in UTC by default; mirror the
        // built-in layout but in browser-local time so the axis agrees
        // with the trades tape and order tables
        tickMarkFormatter: (time: Time, type: TickMarkType) => {
          const d = new Date((time as number) * 1000);
          switch (type) {
            case TickMarkType.Year:
              return String(d.getFullYear());
            case TickMarkType.Month:
              return d.toLocaleDateString(undefined, { month: "short" });
            case TickMarkType.DayOfMonth:
              return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
            default:
              return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
          }
        },
      },
      localization: {
        timeFormatter: (time: Time) => {
          const d = new Date((time as number) * 1000);
          return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
        },
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

    // price content ends above the divider, volume starts below it
    chart.priceScale("right").applyOptions({
      scaleMargins: { top: 0.08, bottom: 1 - PANE_DIVIDER_FRAC + 0.03 },
    });

    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      color: "rgba(14, 165, 233, 0.3)",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: PANE_DIVIDER_FRAC + 0.03, bottom: 0.02 },
    });

    chartRef.current = chart;
    seriesRef.current = series;
    volumeRef.current = volume;
    setChartApi(chart);
    setSeriesApi(series);

    const renderLegend = (c: { time: number; open: number; high: number; low: number; close: number; volume: number } | undefined, tf: string) => {
      const el = legendRef.current;
      if (!el) return;
      if (!c) { el.style.display = "none"; return; }
      el.style.display = "";
      const up = c.close >= c.open;
      const color = up ? "#22c55e" : "#ef4444";
      el.innerHTML =
        `<span style="color:#94a3b8">${fmtTime(c.time, tf)}</span>&nbsp;&nbsp;` +
        `<span style="color:#94a3b8">O</span> <span style="color:${color}">${fmtNum(c.open)}</span>&nbsp;` +
        `<span style="color:#94a3b8">H</span> <span style="color:${color}">${fmtNum(c.high)}</span>&nbsp;` +
        `<span style="color:#94a3b8">L</span> <span style="color:${color}">${fmtNum(c.low)}</span>&nbsp;` +
        `<span style="color:#94a3b8">C</span> <span style="color:${color}">${fmtNum(c.close)}</span>&nbsp;` +
        `<span style="color:#94a3b8">Vol</span> <span style="color:#cbd5e1">${fmtNum(c.volume)}</span>`;
    };

    // hover info: the legend follows the crosshair; without hover it
    // shows the most recent candle (imperative DOM updates to avoid a
    // React re-render on every mouse move)
    chart.subscribeCrosshairMove((param) => {
      const bar = param.time !== undefined && param.point !== undefined
        ? candlesByTimeRef.current.get(param.time as number)
        : [...candlesByTimeRef.current.values()].pop();
      renderLegend(bar, useChartStore.getState().timeframe);
    });

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
      setChartApi(null);
      setSeriesApi(null);
    };
  }, []);

  // Fetch candles when symbol or timeframe changes
  useEffect(() => {
    needFitRef.current = true; // new dataset: viewport must refit to it
    fetchCandles(symbol, timeframe);
  }, [symbol, timeframe, fetchCandles]);

  // Update chart from candle store (batch setData)
  useEffect(() => {
    if (!seriesRef.current || !volumeRef.current) return;

    if (candles.length === 0) {
      seriesRef.current.setData([]);
      volumeRef.current.setData([]);
      lastBarTimeRef.current = 0;
      candlesByTimeRef.current.clear();
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
    candlesByTimeRef.current = new Map(candles.map((c) => [c.time, c]));
    lastBarTimeRef.current = candles[candles.length - 1].time;
    setHasData(true);
    hasCandlesRef.current = true;

    // Prepending an older page shifts logical indices; pin the user's
    // viewport back to the time range they were looking at.
    if (preserveRangeRef.current && chartRef.current) {
      chartRef.current.timeScale().setVisibleRange(preserveRangeRef.current);
      preserveRangeRef.current = null;
    } else if (needFitRef.current && chartRef.current) {
      // fresh dataset (initial load, symbol/timeframe switch): show it all
      chartRef.current.timeScale().fitContent();
      needFitRef.current = false;
    }
  }, [candles]);

  // Infinite history: when the user scrolls to the left edge, fetch the
  // next older page (server pages via the `end` cursor + has_more flag).
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !hasCandlesRef.current) return;
    const ts = chart.timeScale();
    const onRange = (range: LogicalRange | null) => {
      if (!range || range.from > 5) return;
      if (loadingOlderRef.current || !useChartStore.getState().hasMore) return;
      loadingOlderRef.current = true;
      preserveRangeRef.current = ts.getVisibleRange();
      void loadOlder().finally(() => { loadingOlderRef.current = false; });
    };
    ts.subscribeVisibleLogicalRangeChange(onRange);
    return () => ts.unsubscribeVisibleLogicalRangeChange(onRange);
  }, [symbol, timeframe, loadOlder, candles.length === 0]);

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
    <div className="flex h-full min-h-0 flex-col">
      <ChartToolbar
        symbol={symbol}
        timeframe={timeframe}
        onTimeframeChange={setTimeframe}
      />
      <div
        ref={containerRef}
        className="relative flex-1 min-h-0"
        onContextMenu={openMenuAtCursor}
        onClick={() => setMenu(null)}
      >
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
        {chartApi && seriesApi && (
          <DepthOverlay
            chart={chartApi}
            series={seriesApi}
            showBook={depthPrefs.book}
            showStops={depthPrefs.stops}
            showPublic={depthPrefs.publicStops}
            showMyOrders={depthPrefs.myOrders}
          />
        )}
        {/* hovered-candle OHLCV legend (imperatively updated) */}
        <div
          ref={legendRef}
          className="pointer-events-none absolute left-2 top-10 z-20 rounded border border-border bg-panelLight/85 px-2 py-1 font-mono text-[11px] backdrop-blur-sm"
          style={{ display: "none" }}
        />
        {isAuthenticated && (
          <ChartTradePad
            symbol={symbol}
            qty={padQty}
            setQty={setPadQtyPersisted}
            msg={chartMsg}
            setMsg={setChartMsg}
          />
        )}
        {menu && (
          <>
            <div
              className="absolute inset-0 z-30"
              onClick={() => setMenu(null)}
              onContextMenu={(e) => {
                e.preventDefault();
                setMenu(null);
              }}
            />
            <ChartContextMenu
              symbol={symbol}
              qty={padQty}
              menu={menu}
              onDone={setChartMsg}
              onClose={() => setMenu(null)}
            />
          </>
        )}
        {/* depth overlay legend / toggles */}
        <div className="pointer-events-auto absolute left-2 top-2 z-20 flex items-center gap-1.5 rounded border border-border bg-panelLight/80 px-2 py-1 backdrop-blur-sm">
          <button
            onClick={() => toggleDepth("book")}
            title="Глубина стакана на графике"
            className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
              depthPrefs.book ? "text-gray-100" : "text-gray-500"
            } hover:text-white`}
          >
            <span className="inline-block h-2 w-2 rounded-sm bg-emerald-500/80" />
            <span className="inline-block h-2 w-2 rounded-sm bg-red-500/80" />
            Depth
          </button>
          <span className="h-3 w-px bg-border" />
          <button
            onClick={() => toggleDepth("stops")}
            title="Мои стоп-триггеры на графике"
            className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
              depthPrefs.stops ? "text-gray-100" : "text-gray-500"
            } hover:text-white`}
          >
            <span className="inline-block h-2 w-2 rounded-sm bg-amber-400/90" />
            My stops
          </button>
          <span className="h-3 w-px bg-border" />
          <button
            onClick={() => toggleDepth("myOrders")}
            title="Мои активные ордера на графике"
            className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
              depthPrefs.myOrders ? "text-gray-100" : "text-gray-500"
            } hover:text-white`}
          >
            <span className="inline-block h-2 w-2 rounded-sm bg-violet-400/90" />
            My orders
          </button>
          <span className="h-3 w-px bg-border" />
          <button
            onClick={() => toggleDepth("publicStops")}
            title="Условные заявки всех участников (агрегировано)"
            className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
              depthPrefs.publicStops ? "text-gray-100" : "text-gray-500"
            } hover:text-white`}
          >
            <span className="inline-block h-2 w-2 rounded-sm border border-emerald-400 bg-transparent" />
            <span className="inline-block h-2 w-2 rounded-sm border border-red-400 bg-transparent" />
            All stops
          </button>
        </div>
      </div>
    </div>
  );
}
