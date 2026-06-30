/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Trading-specific palette
        bid: "#22c55e",      // green for bids
        ask: "#ef4444",      // red for asks
        bidBg: "rgba(34,197,94,0.15)",
        askBg: "rgba(239,68,68,0.15)",
        panel: "#1a1a2e",
        panelLight: "#16213e",
        border: "#0f3460",
        accent: "#0ea5e9",
      },
      fontFamily: {
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
      },
      animation: {
        "flash-green": "flashGreen 0.3s ease-out",
        "flash-red": "flashRed 0.3s ease-out",
      },
      keyframes: {
        flashGreen: {
          "0%": { backgroundColor: "rgba(34,197,94,0.4)" },
          "100%": { backgroundColor: "transparent" },
        },
        flashRed: {
          "0%": { backgroundColor: "rgba(239,68,68,0.4)" },
          "100%": { backgroundColor: "transparent" },
        },
      },
    },
  },
  plugins: [],
};
