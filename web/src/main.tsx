import "@fontsource-variable/inter";
import "./index.css";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AppToaster } from "./components/AppToaster";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { TooltipProvider } from "./components/ui/tooltip";
import { isAuthError } from "./lib/api";
import { MtApp } from "./mt/MtApp";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: (failures, error) => !isAuthError(error) && failures < 1, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("index.html is missing #root");

/**
 * Which server is this? ferry-web (multi-tenant) answers /auth/config (200, or 503 while its identity
 * provider is unreachable); the single-container app doesn't have it (404).
 */
async function multiTenant(): Promise<boolean> {
  try {
    const response = await fetch("/auth/config", { cache: "no-store", headers: { Accept: "application/json" } });
    return response.status !== 404;
  } catch {
    return false;
  }
}

void multiTenant().then((mt) =>
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ErrorBoundary>{mt ? <MtApp /> : <App />}</ErrorBoundary>
          <AppToaster />
        </TooltipProvider>
      </QueryClientProvider>
    </StrictMode>,
  ),
);
