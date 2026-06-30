import { computeWsSignature } from "@/lib/hmac";
import { getStoredCredentials } from "@/api/client";
import type { OrderUpdateMsg, BalanceUpdateMsg, BulkResultMsg } from "./types";

/**
 * Private WebSocket client for per-user order/balance events.
 *
 * Auth flow:
 *  1. Connect to /api/v1/ws/private
 *  2. Receive "auth_required" message
 *  3. Send {"action":"auth","api_key":...,"timestamp":...,"signature":...}
 *  4. Receive "auth_ok" → start receiving events
 *
 * Auto-reconnects on disconnect.
 */

type PrivateMessageHandler = (msg: OrderUpdateMsg | BalanceUpdateMsg | BulkResultMsg) => void;
type StatusHandler = (status: "connecting" | "authenticating" | "connected" | "disconnected" | "error") => void;

export class PrivateSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: Set<PrivateMessageHandler> = new Set();
  private statusHandlers: Set<StatusHandler> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;
  private shouldReconnect = true;
  private authTimeout: ReturnType<typeof setTimeout> | null = null;

  constructor(wsBase?: string) {
    const base = wsBase || import.meta.env.VITE_WS_BASE || "";
    this.url = `${base}/api/v1/ws/private`;
  }

  connect(): void {
    this.shouldReconnect = true;
    this._doConnect();
  }

  private _doConnect(): void {
    this._notifyStatus("connecting");
    try {
      this.ws = new WebSocket(this.url);
    } catch (err) {
      console.error("[PrivateSocket] Failed to create WebSocket:", err);
      this._notifyStatus("error");
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log("[PrivateSocket] Connected, awaiting auth");
      this._notifyStatus("authenticating");
      this.reconnectAttempts = 0;
      // Set auth timeout
      this.authTimeout = setTimeout(() => {
        console.error("[PrivateSocket] Auth timed out");
        this.ws?.close();
      }, 5000);
    };

    this.ws.onmessage = async (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.event === "auth_required") {
          await this._sendAuth();
        } else if (msg.event === "auth_ok") {
          console.log("[PrivateSocket] Authenticated");
          if (this.authTimeout) {
            clearTimeout(this.authTimeout);
            this.authTimeout = null;
          }
          this._notifyStatus("connected");
        } else if (msg.event === "auth_failed" || msg.event === "auth_timeout") {
          console.error("[PrivateSocket] Auth failed:", msg.event);
          this._notifyStatus("error");
          this.ws?.close();
        } else {
          // Regular event
          this.handlers.forEach((h) => h(msg));
        }
      } catch (err) {
        console.error("[PrivateSocket] Failed to parse message:", err);
      }
    };

    this.ws.onerror = (err) => {
      console.error("[PrivateSocket] Error:", err);
      this._notifyStatus("error");
    };

    this.ws.onclose = () => {
      console.log("[PrivateSocket] Disconnected");
      this._notifyStatus("disconnected");
      if (this.authTimeout) {
        clearTimeout(this.authTimeout);
        this.authTimeout = null;
      }
      if (this.shouldReconnect) {
        this._scheduleReconnect();
      }
    };
  }

  private async _sendAuth(): Promise<void> {
    const creds = getStoredCredentials();
    if (!creds) {
      console.error("[PrivateSocket] No stored credentials");
      this.ws?.close();
      return;
    }

    const timestamp = Math.floor(Date.now() / 1000);
    const signature = await computeWsSignature(creds.apiSecret, creds.apiKey, timestamp);

    this.ws?.send(
      JSON.stringify({
        action: "auth",
        api_key: creds.apiKey,
        timestamp,
        signature,
      }),
    );
  }

  private _scheduleReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error("[PrivateSocket] Max reconnect attempts reached");
      return;
    }
    this.reconnectAttempts++;
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 30000);
    console.log(`[PrivateSocket] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
    setTimeout(() => this._doConnect(), delay);
  }

  private _notifyStatus(status: Parameters<StatusHandler>[0]): void {
    this.statusHandlers.forEach((h) => h(status));
  }

  onMessage(handler: PrivateMessageHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  onStatusChange(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  disconnect(): void {
    this.shouldReconnect = false;
    if (this.authTimeout) {
      clearTimeout(this.authTimeout);
      this.authTimeout = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
