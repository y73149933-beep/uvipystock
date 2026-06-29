import { adminApi } from "./client";
import type { AdminBalance, AdminBalanceAdjustRequest } from "@/types/admin";

export const balancesApi = {
  adjust: (body: AdminBalanceAdjustRequest) =>
    adminApi.post<AdminBalance>("/api/admin/balances/adjust", body),

  getUserBalances: (userId: number) =>
    adminApi.get<AdminBalance[]>(`/api/admin/balances/${userId}`),
};
