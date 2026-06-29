import { useEffect, useRef, useState } from "react";
import { PrivateSocket } from "@/ws/privateSocket";
import { useAuthStore } from "@/store/authStore";
import { useBalanceStore } from "@/store/balanceStore";
import { useOrdersStore } from "@/store/ordersStore";

export type PrivateFeedStatus = "connecting" | "authenticating" | "connected" | "disconnected" | "error";

/**
 * Hook that manages the private WebSocket connection (per-user events).
 * Only connects when the user is authenticated.
 */
export function usePrivateFeed() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const socketRef = useRef<PrivateSocket | null>(null);
  const [status, setStatus] = useState<PrivateFeedStatus>("disconnected");

  const applyBalanceUpdate = useBalanceStore((s) => s.applyBalanceUpdate);
  const applyOrderUpdate = useOrdersStore((s) => s.applyOrderUpdate);

  useEffect(() => {
    if (!isAuthenticated) {
      socketRef.current?.disconnect();
      socketRef.current = null;
      setStatus("disconnected");
      return;
    }

    const socket = new PrivateSocket();
    socketRef.current = socket;

    const unsubMsg = socket.onMessage((msg: any) => {
      switch (msg.event) {
        case "balance":
          applyBalanceUpdate(msg);
          break;
        case "order":
          applyOrderUpdate(msg);
          break;
        case "bulk_result":
          // Could show a toast notification here
          console.log("[PrivateFeed] Bulk result:", msg);
          break;
      }
    });

    const unsubStatus = socket.onStatusChange((s) => setStatus(s));

    socket.connect();

    return () => {
      unsubMsg();
      unsubStatus();
      socket.disconnect();
      socketRef.current = null;
    };
  }, [isAuthenticated, applyBalanceUpdate, applyOrderUpdate]);

  return { status };
}
