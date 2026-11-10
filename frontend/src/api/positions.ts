import { api } from "./client";
import type { Position, PositionListResponse } from "@/types/position";

export const positionsApi = {
  list: () => api.get<PositionListResponse>("/api/v1/positions"),
};

export type { Position };
