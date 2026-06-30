import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: "📊" },
  { to: "/users", label: "Users", icon: "👥" },
  { to: "/balances", label: "Balances", icon: "💰" },
  { to: "/market", label: "Market", icon: "📈" },
  { to: "/emulator", label: "Emulator", icon: "🎲" },
];

export function Sidebar() {
  return (
    <aside className="flex h-full w-56 flex-col border-r border-border bg-panel">
      <div className="flex h-14 items-center border-b border-border px-4">
        <span className="text-lg font-bold text-accent">Admin Panel</span>
      </div>
      <nav className="flex-1 space-y-1 p-2">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-accent text-white"
                  : "text-gray-400 hover:bg-panelLight hover:text-gray-200",
              )
            }
          >
            <span className="text-base">{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </nav>
      <div className="border-t border-border p-2">
        <button
          onClick={() => {
            localStorage.removeItem("admin_jwt");
            window.location.href = "/login";
          }}
          className="w-full rounded px-3 py-2 text-left text-sm text-gray-400 hover:bg-panelLight hover:text-ask"
        >
          🚪 Logout
        </button>
      </div>
    </aside>
  );
}
