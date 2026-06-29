import { cn } from "@/lib/utils";

interface SpinnerProps {
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function Spinner({ size = "md", className }: SpinnerProps) {
  const sizes = {
    sm: "h-3 w-3 border",
    md: "h-5 w-5 border-2",
    lg: "h-8 w-8 border-2",
  };
  return (
    <span
      className={cn(
        "inline-block animate-spin rounded-full border-current border-t-transparent text-accent",
        sizes[size],
        className,
      )}
      role="status"
      aria-label="Loading"
    />
  );
}
