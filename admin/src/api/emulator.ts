import { adminApi } from "./client";
import type {
  AdminEmulatorRandomWalkRequest,
  AdminEmulatorTradeInjectRequest,
} from "@/types/admin";

export const emulatorApi = {
  randomWalk: (body: AdminEmulatorRandomWalkRequest) =>
    adminApi.post<{ job_id: string; status: string }>("/api/admin/emulator/random-walk", body),

  injectTrade: (body: AdminEmulatorTradeInjectRequest) =>
    adminApi.post<{ status: string }>("/api/admin/emulator/trade-inject", body),
};
