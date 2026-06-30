import { type InputHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/utils";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  prefix?: string;
  suffix?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, label, error, prefix, suffix, id, ...props }, ref) => {
    const inputId = id || (label ? `input-${label.replace(/\s+/g, "-").toLowerCase()}` : undefined);
    return (
      <div className="w-full">
        {label && (
          <label htmlFor={inputId} className="mb-1 block text-xs font-medium text-gray-400">
            {label}
          </label>
        )}
        <div className="flex items-stretch">
          {prefix && (
            <span className="flex items-center rounded-l border border-r-0 border-border bg-panel px-2 text-sm text-gray-500">
              {prefix}
            </span>
          )}
          <input
            ref={ref}
            id={inputId}
            className={cn(
              "flex-1 rounded border border-border bg-panel px-3 py-2 text-sm text-gray-100 placeholder:text-gray-600 focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent",
              prefix && "rounded-l-none",
              suffix && "rounded-r-none",
              error && "border-ask",
              className,
            )}
            {...props}
          />
          {suffix && (
            <span className="flex items-center rounded-r border border-l-0 border-border bg-panel px-2 text-sm text-gray-500">
              {suffix}
            </span>
          )}
        </div>
        {error && <p className="mt-1 text-xs text-ask">{error}</p>}
      </div>
    );
  },
);
Input.displayName = "Input";
