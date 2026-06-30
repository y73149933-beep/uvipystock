import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { usersApi } from "@/api/users";
import type { AdminUser } from "@/types/admin";

interface UserFormProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

export function UserForm({ open, onClose, onCreated }: UserFormProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || password.length < 8) {
      setError("Email and password (min 8 chars) required");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await usersApi.create({ email, password, is_admin: isAdmin });
      onCreated();
      onClose();
      setEmail("");
      setPassword("");
      setIsAdmin(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-lg border border-border bg-panel p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="mb-4 text-lg font-semibold">Create User</h3>
        <form onSubmit={handleSubmit} className="space-y-3">
          <Input label="Email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          <Input
            label="Password (min 8 chars)"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={isAdmin}
              onChange={(e) => setIsAdmin(e.target.checked)}
              className="h-4 w-4 accent-accent"
            />
            Admin user
          </label>
          {error && <p className="text-sm text-ask">{error}</p>}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={loading}>
              Create
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
