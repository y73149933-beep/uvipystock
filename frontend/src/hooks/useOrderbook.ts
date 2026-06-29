import { useEffect, useRef } from "react";
import { PublicSocket } from "@/ws/publicSocket";
import { useOrderbookStore } from "@/store/orderbookStore";
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
  const addTrade = useTradesStore((s) => s.addTrade);
  const updateLastPrice = useChartStore((s) => s.updateLastPrice);
  const addTradeToCandle = useChartStore((s) => s.addTradeToCandle);

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
        case "trade":
          addTrade(msg);
          updateLastPrice(msg.price);
          addTradeToCandle(msg.price, msg.quantity);
          break;
      }
    });

    socket.connect();

    return () => {
      unsub();
      socket.disconnect();
      socketRef.current = null;
    };
  }, [symbol, applySnapshot, applyUpdate, addTrade, updateLastPrice, addTradeToCandle]);
}
