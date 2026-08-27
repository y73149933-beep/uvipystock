import { adminApi } from "./client";
import type { AdminTradingPair, AdminTradingPairCreateRequest } from "@/types/admin";

export const marketApi = {
  createPair: (body: AdminTradingPairCreateRequest) =>
    adminApi.post<AdminTradingPair>("/api/admin/market/pairs", body),

  listPairs: (includeInactive?: boolean) =>
    adminApi.get<AdminTradingPair[]>(
      `/api/admin/market/pairs${includeInactive ? "?include_inactive=true" : ""}`,
    ),

  togglePairActive: (pairId: number, isActive: boolean) =>
    adminApi.patch<AdminTradingPair>(`/api/admin/market/pairs/${pairId}/active?is_active=${isActive}`),
};
