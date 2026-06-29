import { useEffect, useRef, useState } from "react";

/**
 * Generic WebSocket subscription hook with auto-reconnect.
 *
 * @param url The WebSocket URL to connect to.
 * @param onMessage Callback invoked for each parsed JSON message.
 * @param enabled If false, the socket is not created.
 */
export function useWebSocket(
  url: string | null,
  onMessage: (msg: any) => void,
  enabled: boolean = true,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<"connecting" | "open" | "closed" | "error">("closed");
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>();
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useEffect(() => {
    if (!enabled || !url) return;

    let closed = false;
    let attempt = 0;

    const connect = () => {
      if (closed) return;
      setStatus("connecting");
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        attempt = 0;
        setStatus("open");
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          onMessageRef.current(msg);
        } catch (err) {
          console.error("useWebSocket: failed to parse message", err);
        }
      };

      ws.onerror = () => {
        setStatus("error");
      };

      ws.onclose = () => {
        setStatus("closed");
        if (!closed) {
          attempt++;
          const delay = Math.min(1000 * 2 ** attempt, 30000);
          reconnectRef.current = setTimeout(connect, delay);
        }
      };
    };

    connect();

    return () => {
      closed = true;
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [url, enabled]);

  return { status, ws: wsRef };
}
