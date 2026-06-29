import type { OrderBookSnapshotMsg, OrderBookUpdateMsg, TradePrintMsg } from "./types";

/**
 * Public WebSocket client for orderbook + trades streams.
 *
 * Auto-reconnects on disconnect with exponential backoff.
 * Sends periodic pings to keep the connection alive.
 */

type MessageHandler = (msg: OrderBookSnapshotMsg | OrderBookUpdateMsg | TradePrintMsg) => void;

export class PublicSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private symbol: string;
  private handlers: Set<MessageHandler> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;
  private reconnectDelay = 1000;
  private pingInterval: ReturnType<typeof setInterval> | null = null;
  private shouldReconnect = true;

  constructor(symbol: string, wsBase?: string) {
    this.symbol = symbol;
    const base = wsBase || import.meta.env.VITE_WS_BASE || "";
    // Use relative path so Vite proxy works in dev
    this.url = `${base}/api/v1/ws/orderbook/${symbol}`;
  }

  connect(): void {
    this.shouldReconnect = true;
    this._doConnect();
  }

  private _doConnect(): void {
    try {
      this.ws = new WebSocket(this.url);
    } catch (err) {
      console.error("[PublicSocket] Failed to create WebSocket:", err);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log(`[PublicSocket] Connected to ${this.symbol}`);
      this.reconnectAttempts = 0;
      this._startPing();
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.handlers.forEach((h) => h(msg));
      } catch (err) {
        console.error("[PublicSocket] Failed to parse message:", err);
      }
    };

    this.ws.onerror = (err) => {
      console.error("[PublicSocket] Error:", err);
    };

    this.ws.onclose = () => {
      console.log(`[PublicSocket] Disconnected from ${this.symbol}`);
      this._stopPing();
      if (this.shouldReconnect) {
        this._scheduleReconnect();
      }
    };
  }

  private _scheduleReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error("[PublicSocket] Max reconnect attempts reached");
      return;
    }
    this.reconnectAttempts++;
    const delay = Math.min(this.reconnectDelay * 2 ** this.reconnectAttempts, 30000);
    console.log(`[PublicSocket] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
    setTimeout(() => this._doConnect(), delay);
  }

  private _startPing(): void {
    this.pingInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ action: "ping" }));
      }
    }, 15000);
  }

  private _stopPing(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  onMessage(handler: MessageHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  disconnect(): void {
    this.shouldReconnect = false;
    this._stopPing();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
