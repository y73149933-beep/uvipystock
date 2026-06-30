import { Link } from "react-router-dom";
import { Button } from "@/components/common/Button";

export function NotFoundPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-panel gap-4">
      <h1 className="text-6xl font-bold text-gray-700">404</h1>
      <p className="text-gray-500">Page not found</p>
      <Link to="/">
        <Button variant="primary">Back to Trading</Button>
      </Link>
    </div>
  );
}
