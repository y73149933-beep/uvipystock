import { create } from "zustand";
import { getStoredCredentials, setStoredCredentials, clearStoredCredentials } from "@/api/client";

interface AuthState {
  apiKey: string | null;
  apiSecret: string | null;
  isAuthenticated: boolean;
  hydrated: boolean;
  login: (apiKey: string, apiSecret: string) => void;
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
  const creds = getStoredCredentials();
  return {
    apiKey: creds?.apiKey ?? null,
    apiSecret: creds?.apiSecret ?? null,
    isAuthenticated: creds !== null,
    hydrated: true,
  };
}

export const useAuthStore = create<AuthState>((set) => ({
  ...getInitialState(),
  login: (apiKey, apiSecret) => {
    setStoredCredentials(apiKey, apiSecret);
    set({ apiKey, apiSecret, isAuthenticated: true, hydrated: true });
  },
  logout: () => {
    clearStoredCredentials();
    set({ apiKey: null, apiSecret: null, isAuthenticated: false });
  },
  hydrate: () => {
    const creds = getStoredCredentials();
    if (creds) {
      set({ apiKey: creds.apiKey, apiSecret: creds.apiSecret, isAuthenticated: true, hydrated: true });
    } else {
      set({ hydrated: true });
    }
  },
}));
