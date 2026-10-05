"use client";
import { RouteErrorPage } from "@/components/shell/error-pages";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return <RouteErrorPage message={error.message} onRetry={reset} />;
}
