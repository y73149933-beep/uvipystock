import { create } from "zustand";
import { getStoredToken, clearStoredToken } from "@/api/client";

interface AdminAuthState {
  token: string | null;
  isAuthenticated: boolean;
  hydrated: boolean;
  setToken: (token: string) => void;
  logout: () => void;
  hydrate: () => void;
}

/**
 * Initialize from localStorage SYNCHRONOUSLY at store creation time.
 *
 * This is critical for F5 refresh: the store must have the correct
 * isAuthenticated value on the FIRST render, before any useEffect runs.
 * Otherwise ProtectedRoute would redirect to /login before hydrate()
 * gets a chance to run.
 */
function getInitialState() {
  const token = getStoredToken();
  return {
    token,
    isAuthenticated: token !== null,
    hydrated: true,
  };
}

export const useAdminStore = create<AdminAuthState>((set) => ({
  ...getInitialState(),
  setToken: (token) => {
    set({ token, isAuthenticated: true, hydrated: true });
  },
  logout: () => {
    clearStoredToken();
    set({ token: null, isAuthenticated: false });
  },
  hydrate: () => {
    const token = getStoredToken();
    if (token) {
      set({ token, isAuthenticated: true, hydrated: true });
    } else {
      set({ hydrated: true });
    }
  },
}));
