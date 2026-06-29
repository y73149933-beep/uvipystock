// Admin domain types matching backend schemas/admin.py

export interface AdminUser {
  id: number;
  email: string;
  is_active: boolean;
  is_admin: boolean;
  created_at: string;
}

export interface AdminBalance {
  user_id: number;
  asset: string;
  total: string;
  locked: string;
  available: string;
  updated_at: string;
}

export interface AdminTradingPair {
  id: number;
  symbol: string;
  base_asset: string;
  quote_asset: string;
  price_precision: number;
  quantity_precision: number;
  min_lot_size: string;
  max_lot_size: string;
  tick_size: string;
  maker_fee_bps: string;
  taker_fee_bps: string;
  is_active: boolean;
  created_at: string;
}

export interface AdminApiKey {
  id: number;
  user_id: number;
  api_key: string;
  label: string | null;
  permissions: string[];
  rate_limit_per_min: number;
  is_revoked: boolean;
  created_at: string;
}

export interface AdminApiKeyWithSecret extends AdminApiKey {
  secret: string;
}

export interface AdminBalanceAdjustRequest {
  user_id: number;
  asset: string;
  delta: string;
  reason: string;
}

export interface AdminUserCreateRequest {
  email: string;
  password: string;
  is_admin: boolean;
}

export interface AdminTradingPairCreateRequest {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  price_precision: number;
  quantity_precision: number;
  min_lot_size: string;
  max_lot_size: string;
  tick_size: string;
  maker_fee_bps: string;
  taker_fee_bps: string;
}

export interface AdminApiKeyCreateRequest {
  user_id: number;
  label?: string;
  permissions: string[];
  rate_limit_per_min: number;
}

export interface AdminEmulatorRandomWalkRequest {
  symbol: string;
  start_price: string;
  volatility_pct: string;
  steps: number;
  interval_ms: number;
}

export interface AdminEmulatorTradeInjectRequest {
  symbol: string;
  price: string;
  quantity: string;
  side: "buy" | "sell";
}
