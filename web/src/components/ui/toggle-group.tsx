import * as ToggleGroup from "@radix-ui/react-toggle-group";

export interface FilterOption<T extends string> {
  value: T;
  label: string;
  count: number;
}

/** A single-choice filter (a toggle group: it changes what's shown in place, there are no tab panels). */
export function FilterGroup<T extends string>({
  value,
  onChange,
  options,
  label,
}: {
  value: T;
  onChange: (value: T) => void;
  options: readonly FilterOption<T>[];
  label: string;
}) {
  return (
    <ToggleGroup.Root
      type="single"
      value={value}
      // Clicking the active item tries to deselect it (an empty value); keep the current filter instead.
      onValueChange={(next) => {
        const option = options.find((o) => o.value === next);
        if (option) onChange(option.value);
      }}
      aria-label={label}
      className="inline-flex rounded-lg border bg-muted p-0.5 text-[13px]"
    >
      {options.map((o) => (
        <ToggleGroup.Item
          key={o.value}
          value={o.value}
          className="h-7 rounded-md px-2.5 font-medium text-muted-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring data-[state=on]:bg-card data-[state=on]:text-foreground data-[state=on]:shadow-sm sm:px-3"
        >
          {o.label}
          <span className="tabular ml-1.5 text-xs text-muted-foreground">{o.count}</span>
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
