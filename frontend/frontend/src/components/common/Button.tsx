import { type ButtonHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/utils";

type Variant = "primary" | "secondary" | "danger" | "success" | "ghost" | "outline";
type Size = "sm" | "md" | "lg";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

const variants: Record<Variant, string> = {
  primary: "bg-accent text-white hover:bg-accent/90 focus:ring-accent",
  secondary: "bg-panelLight text-gray-200 hover:bg-panelLight/80 focus:ring-panelLight",
  danger: "bg-ask text-white hover:bg-ask/90 focus:ring-ask",
  success: "bg-bid text-white hover:bg-bid/90 focus:ring-bid",
  ghost: "text-gray-300 hover:bg-panelLight focus:ring-panelLight",
  outline: "border border-border text-gray-200 hover:bg-panelLight focus:ring-border",
};

const sizes: Record<Size, string> = {
  sm: "px-2 py-1 text-xs",
  md: "px-4 py-2 text-sm",
  lg: "px-6 py-3 text-base",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", size = "md", loading, disabled, children, ...props }, ref) => {
    return (
      <button
        ref={ref}
        disabled={disabled || loading}
        className={cn(
          "rounded font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-offset-panel disabled:opacity-50 disabled:cursor-not-allowed",
          variants[variant],
          sizes[size],
          className,
        )}
        {...props}
      >
        {loading && (
          <span className="mr-2 inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
        )}
        {children}
      </button>
    );
  },
);
Button.displayName = "Button";
