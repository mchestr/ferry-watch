import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center gap-1.5 rounded-full px-2.5 h-6 text-xs font-semibold whitespace-nowrap", {
  variants: {
    variant: {
      success: "bg-success text-success-foreground",
      soft: "bg-success-soft text-success",
      muted: "bg-muted text-muted-foreground",
      outline: "border text-muted-foreground",
      warning: "bg-warning-soft text-warning",
      danger: "bg-danger-soft text-danger",
    },
  },
  defaultVariants: { variant: "muted" },
});

export function Badge({ className, variant, ...props }: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
