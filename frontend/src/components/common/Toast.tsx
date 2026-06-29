import { useEffect, useState, useCallback } from "react";
import { cn } from "@/lib/utils";

export type ToastType = "success" | "error" | "info" | "warning";

interface ToastItem {
  id: number;
  message: string;
  type: ToastType;
}

let toastCounter = 0;
const listeners: Set<(items: ToastItem[]) => void> = new Set();
let items: ToastItem[] = [];

function notify() {
  listeners.forEach((l) => l([...items]));
}

export function showToast(message: string, type: ToastType = "info", duration = 4000) {
  const id = ++toastCounter;
  items = [...items, { id, message, type }];
  notify();
  if (duration > 0) {
    setTimeout(() => {
      items = items.filter((i) => i.id !== id);
      notify();
    }, duration);
  }
}

export function useToast() {
  return { showToast };
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  useEffect(() => {
    const listener = (newItems: ToastItem[]) => setToasts(newItems);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  const remove = useCallback((id: number) => {
    items = items.filter((i) => i.id !== id);
    notify();
  }, []);

  const colors: Record<ToastType, string> = {
    success: "border-bid bg-bid/10 text-bid",
    error: "border-ask bg-ask/10 text-ask",
    info: "border-accent bg-accent/10 text-accent",
    warning: "border-yellow-500 bg-yellow-500/10 text-yellow-500",
  };

  return (
    <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            "flex items-center justify-between gap-4 rounded border px-4 py-2 text-sm shadow-lg",
            colors[t.type],
          )}
        >
          <span>{t.message}</span>
          <button onClick={() => remove(t.id)} className="text-current opacity-60 hover:opacity-100">
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
