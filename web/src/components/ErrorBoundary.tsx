import { AlertTriangle, RotateCw } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "./ui/button";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Catches render errors so the page shows a way out instead of going blank. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error("Unknown error") };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Waitlist crashed", error, info.componentStack);
  }

  override render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div
        role="alert"
        className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-3 px-6 text-center"
      >
        <AlertTriangle className="size-8 text-danger" aria-hidden />
        <h1 className="text-lg font-semibold">Something went wrong</h1>
        <p className="break-words text-sm text-muted-foreground">{error.message}</p>
        <Button className="mt-2" onClick={() => window.location.reload()}>
          <RotateCw aria-hidden /> Reload
        </Button>
      </div>
    );
  }
}
