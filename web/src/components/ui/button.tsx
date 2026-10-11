import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground shadow-sm hover:bg-primary/90",
        success: "bg-success text-success-foreground shadow-sm hover:bg-success/90",
        danger: "bg-danger text-danger-foreground shadow-sm hover:bg-danger/90",
        outline: "border border-input bg-card shadow-sm hover:bg-muted",
        ghost: "hover:bg-muted",
        subtle: "bg-muted text-foreground hover:bg-muted/70",
      },
      size: {
        default: "h-9 px-4",
        sm: "h-8 px-3 text-[13px]",
        lg: "h-11 px-5 text-[15px]",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>;

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, type = "button", ...props }, ref) => (
    <button ref={ref} type={type} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  ),
);
Button.displayName = "Button";

export type LinkButtonProps = React.AnchorHTMLAttributes<HTMLAnchorElement> & VariantProps<typeof buttonVariants>;

/**
 * A link styled as a button. Links opening a new tab get rel="noopener noreferrer" unless told
 * otherwise. Forwards its ref, so it can be a tooltip trigger.
 */
export const LinkButton = React.forwardRef<HTMLAnchorElement, LinkButtonProps>(
  ({ className, variant, size, rel, target, ...props }, ref) => (
    <a
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      target={target}
      rel={rel ?? (target === "_blank" ? "noopener noreferrer" : undefined)}
      {...props}
    />
  ),
);
LinkButton.displayName = "LinkButton";
