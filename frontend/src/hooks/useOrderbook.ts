import { useEffect, useRef } from "react";
import { PublicSocket } from "@/ws/publicSocket";
import { useOrderbookStore } from "@/store/orderbookStore";
import { useStopLadderStore } from "@/store/stopLadderStore";
import { useTradesStore } from "@/store/tradesStore";
import { useChartStore } from "@/store/chartStore";

/**
 * Hook that manages the public orderbook WebSocket connection.
 * Automatically reconnects when the symbol changes.
 */
export function useOrderbookFeed(symbol: string) {
  const socketRef = useRef<PublicSocket | null>(null);
  const applySnapshot = useOrderbookStore((s) => s.applySnapshot);
  const applyUpdate = useOrderbookStore((s) => s.applyUpdate);
  const applyTradeOptimistic = useOrderbookStore((s) => s.applyTradeOptimistic);
  const addTrade = useTradesStore((s) => s.addTrade);
  const updateLastPrice = useChartStore((s) => s.updateLastPrice);
  const addTradeToCandle = useChartStore((s) => s.addTradeToCandle);
  const fetchLadder = useStopLadderStore((s) => s.fetchLadder);

  useEffect(() => {
    const socket = new PublicSocket(symbol);
    socketRef.current = socket;

    const unsub = socket.onMessage((msg) => {
      switch (msg.event) {
        case "orderbook_snapshot":
          applySnapshot(msg);
          break;
        case "orderbook_update":
          applyUpdate(msg);
          break;
        case "stops_changed":
          // Conditional-order ladder changed on the server — refetch it.
          if (msg.symbol === symbol) {
            void fetchLadder(symbol);
          }
          break;
        case "reconnected":
          // Recover everything the offline period swallowed: the trades
          // tape is replaced from the public endpoint (dedupe by
          // trade_id is handled by refetch), candles merged by bucket.
          if (msg.symbol === symbol) {
            console.log("[feed] WS reconnected — backfilling trades + candles");
            void useTradesStore.getState().fetchRecent(symbol);
            void useChartStore.getState().syncRecent();
          }
          break;

        case "trade":
          addTrade(msg);
          applyTradeOptimistic(msg.price, msg.quantity, msg.side);
          // bucket by the server print timestamp, not the client clock
          updateLastPrice(msg.price, msg.ts);
          addTradeToCandle(msg.price, msg.quantity, msg.ts);
          break;
      }
    });

    socket.connect();

    return () => {
      unsub();
      socket.disconnect();
      socketRef.current = null;
    };
  }, [symbol, applySnapshot, applyUpdate, applyTradeOptimistic, addTrade, updateLastPrice, addTradeToCandle, fetchLadder]);
}
