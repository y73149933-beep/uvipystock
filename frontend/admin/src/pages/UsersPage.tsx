import { AdminLayout } from "@/components/layout/AdminLayout";
import { UserList } from "@/components/users/UserList";

export function UsersPage() {
  return (
    <AdminLayout>
      <UserList />
    </AdminLayout>
  );
}
