export interface Position {
  symbol: string;
  base: string;
  quantity: number;
  balance: number;
  locked: number;
  avg_cost: number;
  last_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
  position_value: number;
}

export interface PositionListResponse {
  positions: Position[];
}
