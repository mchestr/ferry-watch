import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { useFieldControl } from "./field-context";

export interface Option {
  value: string;
  label: string;
  hint?: string;
}

/**
 * Inside a Field, the trigger takes the field's id, so the field's <label> names it. Outside one,
 * pass `aria-label`.
 */
export function Select({
  value,
  onValueChange,
  options,
  placeholder,
  className,
  disabled,
  "aria-label": ariaLabel,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: Option[];
  placeholder?: string;
  className?: string;
  disabled?: boolean;
  "aria-label"?: string;
}) {
  const field = useFieldControl();
  return (
    <SelectPrimitive.Root
      value={value}
      // Never a choice: Radix forbids "" as an item's value. In a form, Radix's hidden <select> reports ""
      // when the value and the options change together (it gets the new options a render late).
      onValueChange={(next) => {
        if (next !== "") onValueChange(next);
      }}
      disabled={disabled}
    >
      <SelectPrimitive.Trigger
        {...field}
        aria-label={ariaLabel}
        className={cn(
          "flex h-10 w-full items-center justify-between gap-2 rounded-lg border border-input bg-card px-3 text-left text-base shadow-sm transition-colors focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/40 disabled:opacity-50 aria-[invalid=true]:border-danger data-[placeholder]:text-muted-foreground sm:text-sm [&>span]:truncate",
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon asChild>
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={6}
          className="z-[60] max-h-[min(22rem,var(--radix-select-content-available-height))] w-[var(--radix-select-trigger-width)] overflow-hidden rounded-xl border bg-popover text-popover-foreground shadow-xl motion-safe:data-[state=open]:animate-in motion-safe:data-[state=open]:fade-in-0 motion-safe:data-[state=open]:zoom-in-95"
        >
          <SelectPrimitive.Viewport className="p-1">
            {options.map((o) => (
              <SelectPrimitive.Item
                key={o.value}
                value={o.value}
                className="relative flex cursor-pointer select-none flex-col rounded-md py-2 pl-8 pr-3 text-base outline-none data-[highlighted]:bg-muted data-[state=checked]:font-semibold sm:text-sm"
              >
                <span className="absolute left-2.5 top-2.5">
                  <SelectPrimitive.ItemIndicator>
                    <Check className="size-4 text-primary" aria-hidden />
                  </SelectPrimitive.ItemIndicator>
                </span>
                <SelectPrimitive.ItemText>{o.label}</SelectPrimitive.ItemText>
                {o.hint && <span className="text-xs font-normal text-muted-foreground">{o.hint}</span>}
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}
