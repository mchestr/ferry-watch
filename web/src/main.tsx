import "@fontsource-variable/inter";
import "./index.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppToaster } from "./components/AppToaster";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { TooltipProvider } from "./components/ui/tooltip";
import { createQueryClient } from "./lib/query";
import { MtApp } from "./mt/MtApp";

const queryClient = createQueryClient();

const root = document.getElementById("root");
if (!root) throw new Error("index.html is missing #root");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ErrorBoundary>
          <MtApp />
        </ErrorBoundary>
        <AppToaster />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
