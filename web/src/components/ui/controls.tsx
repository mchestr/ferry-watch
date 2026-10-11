import * as RadioGroup from "@radix-ui/react-radio-group";
import { Minus, Plus } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/utils";
import { FieldContext, useFieldControl, type FieldControlProps } from "./field-context";

/**
 * A labelled form field. Renders a real <label htmlFor> and hands the matching id (plus
 * aria-invalid / aria-describedby for the error and hint) to the Input or Select inside it.
 */
export function Field({
  label,
  hint,
  error,
  id,
  className,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  error?: string | null;
  /** Defaults to a generated id. Pass one to focus the control from outside. */
  id?: string;
  className?: string;
  children: React.ReactNode;
}) {
  const generated = React.useId();
  const controlId = id ?? generated;
  const errorId = `${controlId}-error`;
  const hintId = `${controlId}-hint`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ") || undefined;
  const control = React.useMemo<FieldControlProps>(
    () => ({ id: controlId, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined }),
    [controlId, describedBy, error],
  );
  return (
    <div className={cn("space-y-1.5", className)}>
      <label htmlFor={controlId} className="block text-[13px] font-medium text-foreground/80">
        {label}
      </label>
      <FieldContext.Provider value={control}>{children}</FieldContext.Provider>
      {error && (
        <p id={errorId} className="text-xs font-medium text-danger">
          {error}
        </p>
      )}
      {hint && (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
}

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => {
    const field = useFieldControl();
    return (
      <input
        ref={ref}
        {...field}
        {...props}
        className={cn(
          // 16px text on phones: iOS Safari zooms into anything smaller when it's focused.
          "flex h-10 w-full rounded-lg border border-input bg-card px-3 text-base shadow-sm transition-colors placeholder:text-muted-foreground focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/40 disabled:opacity-50 aria-[invalid=true]:border-danger sm:text-sm [color-scheme:light] dark:[color-scheme:dark]",
          className,
        )}
      />
    );
  },
);
Input.displayName = "Input";

/**
 * A −/+ counter. The value sits in a polite live region so changes are announced, and the buttons
 * use aria-disabled at the limits: a disabled button would drop keyboard focus.
 */
export function Stepper({
  value,
  onChange,
  min = 0,
  max = 9,
  singular,
  plural,
  "aria-labelledby": labelledBy,
  "aria-describedby": describedBy,
}: {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  /** "adult": used in "Add adult" and the announced "1 adult". */
  singular: string;
  plural: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
}) {
  return (
    <div
      role="group"
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      className="inline-flex h-10 items-center rounded-lg border border-input bg-card shadow-sm"
    >
      <StepButton label={`Remove ${singular}`} disabled={value <= min} onClick={() => onChange(value - 1)}>
        <Minus aria-hidden />
      </StepButton>
      <span aria-live="polite" aria-atomic="true" className="tabular w-7 text-center text-sm font-semibold">
        {value}
        <span className="sr-only"> {value === 1 ? singular : plural}</span>
      </span>
      <StepButton label={`Add ${singular}`} disabled={value >= max} onClick={() => onChange(value + 1)}>
        <Plus aria-hidden />
      </StepButton>
    </div>
  );
}

function StepButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-disabled={disabled || undefined}
      onClick={() => {
        if (!disabled) onClick();
      }}
      className="grid h-full w-10 place-items-center rounded-lg text-muted-foreground transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring aria-disabled:cursor-not-allowed aria-disabled:opacity-30 aria-disabled:hover:text-muted-foreground [&_svg]:size-4"
    >
      {children}
    </button>
  );
}

export interface Choice<T extends string> {
  value: T;
  label: string;
  icon?: React.ReactNode;
}

/** A segmented single choice: a radio group, with arrow keys moving between options. */
export function ChoiceGroup<T extends string>({
  value,
  onChange,
  options,
  "aria-labelledby": labelledBy,
}: {
  value: T;
  onChange: (value: T) => void;
  options: readonly Choice<T>[];
  "aria-labelledby": string;
}) {
  return (
    <RadioGroup.Root
      value={value}
      onValueChange={(next) => {
        const choice = options.find((o) => o.value === next);
        if (choice) onChange(choice.value);
      }}
      aria-labelledby={labelledBy}
      className="grid auto-cols-fr grid-flow-col gap-1 rounded-xl border bg-muted p-1"
    >
      {options.map((o) => (
        <RadioGroup.Item
          key={o.value}
          value={o.value}
          className="flex h-9 items-center justify-center gap-1.5 rounded-lg text-[13px] font-medium text-muted-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring data-[state=checked]:bg-card data-[state=checked]:font-semibold data-[state=checked]:text-foreground data-[state=checked]:shadow-sm [&_svg]:size-4"
        >
          {o.icon}
          {o.label}
        </RadioGroup.Item>
      ))}
    </RadioGroup.Root>
  );
}
