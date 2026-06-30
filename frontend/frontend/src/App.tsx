import { Routes, Route, Navigate } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";
import { LoginPage } from "@/pages/LoginPage";
import { RegisterPage } from "@/pages/RegisterPage";
import { TradingPage } from "@/pages/TradingPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { ToastContainer } from "@/components/common/Toast";

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  // hydrated is always true now (store initializes synchronously from localStorage),
  // but we keep the check for safety / future async hydration scenarios.
  const hydrated = useAuthStore((s) => s.hydrated);

  if (!hydrated) {
    // Brief loading screen while checking auth state
    return (
      <div className="flex min-h-screen items-center justify-center bg-panel">
        <div className="text-gray-500">Loading...</div>
      </div>
    );
  }

  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  // Store initializes synchronously from localStorage, so no useEffect needed.
  // hydrate() is available for manual refresh if needed (e.g., after tab focus).
  return (
    <>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <TradingPage />
            </ProtectedRoute>
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
      <ToastContainer />
    </>
  );
}
