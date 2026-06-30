import { useEffect, useState } from "react";
import { usersApi } from "@/api/users";
import type { AdminUser } from "@/types/admin";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { UserForm } from "./UserForm";
import { UserDetail } from "./UserDetail";
import { formatTime } from "@/lib/utils";
import { cn } from "@/lib/utils";

export function UserList() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [selectedUser, setSelectedUser] = useState<AdminUser | null>(null);

  const fetchUsers = async () => {
    setLoading(true);
    try {
      const resp = await usersApi.list(0, 200);
      setUsers(resp.users);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  const filtered = users.filter((u) =>
    u.email.toLowerCase().includes(search.toLowerCase()),
  );

  const handleToggleActive = async (user: AdminUser) => {
    try {
      await usersApi.toggleActive(user.id, !user.is_active);
      await fetchUsers();
    } catch (err) {
      alert((err as Error).message);
    }
  };

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xl font-semibold">Users ({filtered.length})</h2>
        <Button onClick={() => setShowForm(true)}>+ Create User</Button>
      </div>

      <div className="mb-4">
        <Input
          placeholder="Search by email..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {error && <div className="mb-4 rounded border border-ask bg-ask/10 p-3 text-sm text-ask">{error}</div>}

      <div className="overflow-hidden rounded border border-border">
        <table className="w-full text-sm">
          <thead className="bg-panel">
            <tr className="text-left text-gray-400">
              <th className="px-4 py-2">ID</th>
              <th className="px-4 py-2">Email</th>
              <th className="px-4 py-2">Role</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2">Created</th>
              <th className="px-4 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-gray-500">
                  Loading...
                </td>
              </tr>
            ) : filtered.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-gray-500">
                  No users found
                </td>
              </tr>
            ) : (
              filtered.map((user) => (
                <tr
                  key={user.id}
                  className="cursor-pointer border-t border-border hover:bg-panel"
                  onClick={() => setSelectedUser(user)}
                >
                  <td className="px-4 py-2 font-mono text-gray-500">{user.id}</td>
                  <td className="px-4 py-2">{user.email}</td>
                  <td className="px-4 py-2">
                    <span
                      className={cn(
                        "rounded px-2 py-0.5 text-xs",
                        user.is_admin ? "bg-accent/20 text-accent" : "bg-panelLight text-gray-400",
                      )}
                    >
                      {user.is_admin ? "Admin" : "User"}
                    </span>
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={cn(
                        "rounded px-2 py-0.5 text-xs",
                        user.is_active ? "bg-bid/20 text-bid" : "bg-ask/20 text-ask",
                      )}
                    >
                      {user.is_active ? "Active" : "Blocked"}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-gray-500">{formatTime(user.created_at)}</td>
                  <td className="px-4 py-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleToggleActive(user);
                      }}
                    >
                      {user.is_active ? "Block" : "Activate"}
                    </Button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <UserForm open={showForm} onClose={() => setShowForm(false)} onCreated={fetchUsers} />
      <UserDetail user={selectedUser} onClose={() => setSelectedUser(null)} />
    </div>
  );
}
