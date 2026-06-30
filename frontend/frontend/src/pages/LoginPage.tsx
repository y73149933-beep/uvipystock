import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Input } from "@/components/common/Input";
import { Button } from "@/components/common/Button";
import { useToast } from "@/components/common/Toast";
import { useAuthStore } from "@/store/authStore";
import { setStoredCredentials } from "@/api/client";

interface LoginApiResponse {
  user_id: number;
  login: string;
  is_admin: boolean;
  api_key: string;
  api_secret: string;
  permissions: string[];
}

export function LoginPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const login = useAuthStore((s) => s.login);
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!loginName || !password) {
      showToast("Login and password are required", "error");
      return;
    }
    setLoading(true);
    try {
      const API_BASE = import.meta.env.VITE_API_BASE || "";
      const resp = await fetch(`${API_BASE}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login: loginName, password }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error?.message || "Login failed");
      }
      const data: LoginApiResponse = await resp.json();
      setStoredCredentials(data.api_key, data.api_secret);
      login(data.api_key, data.api_secret);
      showToast(`Welcome, ${data.login}!`, "success");
      navigate("/");
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
          <h1 className="text-xl font-bold text-gray-100">Exchange Sandbox</h1>
          <p className="mt-1 text-sm text-gray-500">Sign in to start trading</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <Input label="Login" type="text" value={loginName}
            onChange={(e) => setLoginName(e.target.value)} placeholder="test" autoComplete="username" />
          <Input label="Password" type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" autoComplete="current-password" />
          <Button type="submit" loading={loading} className="w-full" size="lg">Sign In</Button>
        </form>
        <p className="mt-4 text-center text-sm text-gray-500">
          Don't have an account?{" "}
          <Link to="/register" className="text-accent hover:underline">Register</Link>
        </p>
        <div className="mt-6 rounded border border-border/50 bg-panel/50 p-3 text-xs text-gray-600">
          <p className="font-medium text-gray-500">Demo accounts:</p>
          <p className="mt-1">test / test</p>
          <p>test2 / test</p>
          <p>admin / admin123</p>
        </div>
      </div>
    </div>
  );
}
