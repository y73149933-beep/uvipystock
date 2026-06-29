import { api } from "./client";
import type { Balance } from "@/types/balance";

interface BalanceListResponse {
  balances: Balance[];
}

export const balanceApi = {
  get: (asset?: string) =>
    api.get<BalanceListResponse>("/api/v1/balance", asset ? { asset } : undefined),
};
