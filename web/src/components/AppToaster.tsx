import { Toaster } from "sonner";
import { useMediaQuery } from "@/lib/hooks";
import { useTheme } from "@/lib/theme";

/** Toasts at the top on phones (clear of the keyboard and the browser's toolbar), bottom-centre otherwise. */
export function AppToaster() {
  const phone = useMediaQuery("(max-width: 639px)");
  const { resolved } = useTheme();
  return <Toaster position={phone ? "top-center" : "bottom-center"} theme={resolved} richColors closeButton />;
}
