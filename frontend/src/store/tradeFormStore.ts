/**
 * Bridge between the order book and the trade form.
 *
 * The trade form keeps its fields in local useState, so a click in the
 * order book cannot call setState directly. This tiny signal store carries
 * the request across component boundaries:
 *
 *   OrderBookRow click -> requestPriceFill(price) -> pendingPriceFill{nonce}
 *   TradeForm useEffect(nonce) -> writes into price / stop_price field
 *
 * The monotonically growing `nonce` guarantees the effect fires even when
 * the SAME price is clicked twice in a row (referential change only).
 */
import { create } from "zustand";

export interface PriceFillSignal {
  /** Clicked price, raw string ready to be put into an input. */
  price: string;
  /** Increases on every request — stable dependency for React effects. */
  nonce: number;
}

interface TradeFormBridgeState {
  pendingPriceFill: PriceFillSignal | null;
  requestPriceFill: (price: number | string) => void;
}

let fillNonce = 0;

export const useTradeFormStore = create<TradeFormBridgeState>((set) => ({
  pendingPriceFill: null,
  requestPriceFill: (price) =>
    set({
      pendingPriceFill: {
        price: typeof price === "number" ? String(price) : price,
        nonce: ++fillNonce,
      },
    }),
}));
