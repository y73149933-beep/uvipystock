/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bid: "#22c55e",
        ask: "#ef4444",
        panel: "#1e293b",
        panelLight: "#334155",
        border: "#475569",
        accent: "#6366f1",
      },
    },
  },
  plugins: [],
};
