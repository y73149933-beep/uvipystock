import { type InputHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/utils";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, label, error, id, ...props }, ref) => {
    const inputId = id || (label ? `input-${label.replace(/\s+/g, "-").toLowerCase()}` : undefined);
    return (
      <div className="w-full">
        {label && (
          <label htmlFor={inputId} className="mb-1 block text-xs font-medium text-gray-400">
            {label}
          </label>
        )}
        <input
          ref={ref}
          id={inputId}
          className={cn(
            "w-full rounded border border-border bg-panel px-3 py-2 text-sm text-gray-100 placeholder:text-gray-600 focus:border-accent focus:outline-none",
            error && "border-ask",
            className,
          )}
          {...props}
        />
        {error && <p className="mt-1 text-xs text-ask">{error}</p>}
      </div>
    );
  },
);
Input.displayName = "Input";
