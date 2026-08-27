import { useEffect, useRef } from "react";
import type { IChartApi, ISeriesApi } from "lightweight-charts";
import { useOrderbookStore } from "@/store/orderbookStore";
import { useOrdersStore } from "@/store/ordersStore";
import { useStopLadderStore } from "@/store/stopLadderStore";

/**
 * Horizontal depth histogram drawn directly over the candlestick chart.
 *
 * Design:
 *  - Every price level of the public order book becomes a horizontal bar
 *    anchored at the right edge of the plotting area (next to the price axis).
 *    Bar length is proportional to volume relative to the deepest level.
 *  - Each side has its own color: bids — emerald, asks — red. The bars are
 *    painted in descending-volume order (painter's algorithm), so smaller
 *    volumes always end up ON TOP of larger ones where they overlap.
 *  - The AGGREGATED conditional-order ladder of ALL users ("stop book",
 *    anonymized by the backend) is drawn as dashed hollow bars, scaled by
 *    their total remaining volume per trigger level. Trailing rows track
 *    their drifted server-side trigger. This shows where deferred orders
 *    of other participants are parked.
 *  - The user's OWN conditional orders (stop_market / stop_limit) are
 *    additionally highlighted as amber dashed trigger lines with a label,
 *    drawn ON TOP of everything.
 *  - The overlay tracks the real price scale via series.priceToCoordinate,
 *    so bars follow panning / zooming / auto-scaling of candles.
 */

interface DepthBar {
  kind: "bid" | "ask";
  price: number;
  volume: number;
}

interface StopMarker {
  price: number;
  label: string;
}

interface PublicStopRow {
  kind: "bid" | "ask";
  price: number;
  volume: number;
  count: number;
  label: string;
}

interface DepthOverlayProps {
  chart: IChartApi;
  series: ISeriesApi<"Candlestick">;
  showBook: boolean;
  showStops: boolean;
  /** Aggregated conditional orders of ALL users ("stop book"). */
  showPublic: boolean;
}

const STOP_TYPES = ["stop_market", "stop_limit"] as const;

const BID_COLOR = (a: number) => `rgba(34, 197, 94, ${a})`;
const ASK_COLOR = (a: number) => `rgba(239, 68, 68, ${a})`;
const BID_TEXT_COLOR = "#4ade80";
const ASK_TEXT_COLOR = "#f87171";
const STOP_COLOR = "rgba(251, 191, 36, 0.8)";
const STOP_TEXT_COLOR = "#fbbf24";

// fallbacks in case axis measurement is unavailable
const FALLBACK_RIGHT_AXIS_W = 62;
const FALLBACK_TIME_AXIS_H = 30;

function safeRightAxisWidth(chart: IChartApi): number {
  try {
    return chart.priceScale("right").width();
  } catch {
    return FALLBACK_RIGHT_AXIS_W;
  }
}

function safeTimeAxisHeight(chart: IChartApi): number {
  try {
    return chart.timeScale().height();
  } catch {
    return FALLBACK_TIME_AXIS_H;
  }
}

function chip(type: string): string {
  if (type === "stop_limit") return "LMT";
  if (type === "trailing_stop") return "TRAIL";
  return "MKT"; // stop_market
}

export function DepthOverlay({
  chart,
  series,
  showBook,
  showStops,
  showPublic,
}: DepthOverlayProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const flagsRef = useRef({ showBook, showStops, showPublic });

  // keep toggle flags available to the animation loop without resubscribing
  useEffect(() => {
    flagsRef.current = { showBook, showStops, showPublic };
  }, [showBook, showStops, showPublic]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const parent = canvas.parentElement as HTMLElement;

    let disposed = false;
    let frame = 0;
    const cssSize = { w: parent.clientWidth, h: parent.clientHeight };

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      cssSize.w = parent.clientWidth;
      cssSize.h = parent.clientHeight;
      canvas.width = Math.max(1, Math.round(cssSize.w * dpr));
      canvas.height = Math.max(1, Math.round(cssSize.h * dpr));
    };
    const ro = new ResizeObserver(resize);
    ro.observe(parent);
    resize();

    let frameCount = 0;
    const drawFrame = () => {
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssSize.w, cssSize.h);

      const {
        showBook: withBook,
        showStops: withStops,
        showPublic: withPublic,
      } = flagsRef.current;
      const ob = useOrderbookStore.getState();
      const paneW = Math.max(0, cssSize.w - safeRightAxisWidth(chart));
      const paneH = Math.max(0, cssSize.h - safeTimeAxisHeight(chart));
      const anchorX = paneW; // bars grow leftwards from the price axis
      const maxBarW = Math.min(paneW * 0.55, 320);

      const toY = (price: number): number | null => {
        let y: number | null = null;
        try {
          y = series.priceToCoordinate(price);
        } catch {
          return null;
        }
        if (y === null) return null;
        return y as unknown as number;
      };

      // ---- book depth bars ---------------------------------------------
      if (withBook && paneW > 0) {
        const levels: DepthBar[] = [];
        for (const l of ob.bids) levels.push({ kind: "bid", price: l.price, volume: l.volume });
        for (const l of ob.asks) levels.push({ kind: "ask", price: l.price, volume: l.volume });

        let maxVol = 0;
        for (const l of levels) if (l.volume > maxVol) maxVol = l.volume;

        if (maxVol > 0 && levels.length > 0) {
          // painter's algorithm: largest first -> smallest stays visible on top
          const ordered = levels.slice().sort((a, b) => b.volume - a.volume);

          // bright accent for best bid / best ask rows
          const bestBidPrice = ob.bids[0]?.price;
          const bestAskPrice = ob.asks[0]?.price;

          for (const l of ordered) {
            const y = toY(l.price);
            if (y === null || y < -3 || y > paneH + 3) continue;
            const rel = l.volume / maxVol;
            const w = Math.max(2, rel * maxBarW);
            const isBest = l.price === bestBidPrice || l.price === bestAskPrice;
            const alpha = (isBest ? 0.85 : 0.5) * (0.55 + 0.45 * rel);
            ctx.fillStyle = l.kind === "bid" ? BID_COLOR(alpha) : ASK_COLOR(alpha);
            ctx.fillRect(anchorX - w, y - 1.5, w, 3);
          }
        }
      }

      // ---- public conditional ladder (all users, aggregated) -------------
      if (withPublic && paneW > 0) {
        const sl = useStopLadderStore.getState();
        if (sl.fetchedSymbol === ob.symbol && sl.levels.length > 0) {
          const rows: PublicStopRow[] = [];
          let maxPubVol = 0;
          for (const lvl of sl.levels) {
            const p = Number(lvl.price);
            const v = Number(lvl.volume);
            if (!Number.isFinite(p) || !Number.isFinite(v)) continue;
            if (p <= 0 || v <= 0) continue;
            const arrow = lvl.side === "buy" ? "▲" : "▼";
            const cnt = lvl.count > 1 ? `×${lvl.count}` : "";
            rows.push({
              kind: lvl.side === "buy" ? "bid" : "ask",
              price: p,
              volume: v,
              count: lvl.count,
              label: `${arrow} ${chip(lvl.type)}${cnt}`,
            });
            if (v > maxPubVol) maxPubVol = v;
          }

          if (maxPubVol > 0 && rows.length > 0) {
            ctx.font = "600 9px 'JetBrains Mono', monospace";
            ctx.textBaseline = "alphabetic";
            // largest first so bigger levels never hide smaller ones
            const ordered = rows.slice().sort((a, b) => b.volume - a.volume);
            for (const row of ordered) {
              const y = toY(row.price);
              if (y === null || y < -3 || y > paneH + 3) continue;
              const rel = row.volume / maxPubVol;
              const w = Math.max(8, rel * maxBarW);

              ctx.save();
              // hollow dashed bar
              ctx.strokeStyle =
                row.kind === "bid" ? BID_COLOR(0.75) : ASK_COLOR(0.75);
              ctx.lineWidth = 1.5;
              ctx.setLineDash([4, 3]);
              ctx.strokeRect(anchorX - w, y - 2.5, w, 5);
              ctx.setLineDash([]);

              // label to the LEFT of the bar start
              const txt = row.label;
              const tw = ctx.measureText(txt).width;
              const tx = Math.max(4, anchorX - w - tw - 4);
              ctx.fillStyle =
                row.kind === "bid" ? BID_TEXT_COLOR : ASK_TEXT_COLOR;
              ctx.fillText(txt, tx, y - 4);
              ctx.restore();
            }
          }
        }
      }

      // ---- own stop trigger lines ---------------------------------------
      if (withStops && paneW > 0) {
        const orders = useOrdersStore.getState().openOrders;
        const markers: StopMarker[] = [];
        for (const o of orders) {
          if (o.symbol !== ob.symbol) continue;
          if (!(STOP_TYPES as readonly string[]).includes(o.type)) continue; // trailing_stop has no static price server-side
          if (o.stop_price === null) continue;
          const p = Number(o.stop_price);
          if (!Number.isFinite(p) || p <= 0) continue;
          // buy-stops trigger above the market, sell-stops below
          const arrow = o.side === "buy" ? "▲" : "▼";
          const kind = o.type === "stop_market" ? "S.MKT" : "S.LMT";
          const label = `${arrow} ${kind} ${o.side.toUpperCase()}`;
          markers.push({ price: p, label });
        }

        ctx.font = "600 9px 'JetBrains Mono', monospace";
        ctx.textBaseline = "alphabetic";
        for (const m of markers) {
          const y = toY(m.price);
          if (y === null || y < 4 || y > paneH - 2) continue;
          ctx.save();
          ctx.strokeStyle = STOP_COLOR;
          ctx.lineWidth = 1;
          ctx.setLineDash([6, 4]);
          ctx.beginPath();
          ctx.moveTo(4, y + 0.5);
          ctx.lineTo(anchorX - 4, y + 0.5);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = STOP_TEXT_COLOR;
          ctx.fillText(m.label, 6, y - 3);
          ctx.restore();
        }
      }
    };

    const loop = () => {
      if (disposed) return;
      // Redraw every other frame (~30fps): coordinates may change not only on
      // pan/zoom but also on silent auto-scale adjustments after new candles.
      if (frameCount++ % 2 === 0) drawFrame();
      frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      ro.disconnect();
    };
  }, [chart, series]);

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 z-10 h-full w-full"
      aria-hidden="true"
    />
  );
}
