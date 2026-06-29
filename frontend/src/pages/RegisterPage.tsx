import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Input } from "@/components/common/Input";
import { Button } from "@/components/common/Button";
import { useToast } from "@/components/common/Toast";

export function RegisterPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!loginName || !password) { showToast("Login and password are required", "error"); return; }
    if (loginName.length < 2) { showToast("Login must be at least 2 characters", "error"); return; }
    if (password.length < 4) { showToast("Password must be at least 4 characters", "error"); return; }
    if (password !== confirmPassword) { showToast("Passwords do not match", "error"); return; }

    setLoading(true);
    try {
      const API_BASE = import.meta.env.VITE_API_BASE || "";
      const resp = await fetch(`${API_BASE}/api/v1/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login: loginName, password }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error?.message || "Registration failed");
      }
      showToast("Account created! Please sign in.", "success");
      navigate("/login");
    } catch (err) {
      showToast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-panel">
      <div className="w-full max-w-sm rounded-lg border border-border bg-panelLight p-8">
        <div className="mb-6 text-center">
          <div className="mb-2 text-3xl">₿</div>
          <h1 className="text-xl font-bold text-gray-100">Create Account</h1>
          <p className="mt-1 text-sm text-gray-500">Register to start paper trading</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <Input label="Login (min 2 chars)" type="text" value={loginName}
            onChange={(e) => setLoginName(e.target.value)} placeholder="my_username" autoComplete="username" />
          <Input label="Password (min 4 chars)" type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
          <Input label="Confirm Password" type="password" value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
          <Button type="submit" loading={loading} className="w-full" size="lg">Register</Button>
        </form>
        <p className="mt-4 text-center text-sm text-gray-500">
          Already have an account?{" "}
          <Link to="/login" className="text-accent hover:underline">Sign In</Link>
        </p>
      </div>
    </div>
  );
}
