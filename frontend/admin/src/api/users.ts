import { adminApi } from "./client";
import type { AdminUser, AdminUserCreateRequest } from "@/types/admin";

interface UserListResponse {
  users: AdminUser[];
  pagination: { offset: number; limit: number; count: number };
}

export const usersApi = {
  create: (body: AdminUserCreateRequest) =>
    adminApi.post<AdminUser>("/api/admin/users", body),

  list: (offset = 0, limit = 50) =>
    adminApi.get<UserListResponse>(`/api/admin/users?offset=${offset}&limit=${limit}`),

  get: (userId: number) =>
    adminApi.get<AdminUser>(`/api/admin/users/${userId}`),

  toggleActive: (userId: number, isActive: boolean) =>
    adminApi.patch<AdminUser>(`/api/admin/users/${userId}/active?is_active=${isActive}`),
};
