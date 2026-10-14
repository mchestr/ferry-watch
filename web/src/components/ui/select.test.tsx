import { screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/render";
import { Select, type Option } from "./select";

const FRUIT: Option[] = [
  { value: "apple", label: "Apple" },
  { value: "pear", label: "Pear" },
];
const VEG: Option[] = [{ value: "leek", label: "Leek" }];

/** In a form, like the trip sheet: Radix keeps a hidden native <select> in step with the value. */
function Harness({ onValueChange }: { onValueChange: (value: string) => void }) {
  const [options, setOptions] = useState<Option[]>([]);
  const [value, setValue] = useState("");
  const pick = (next: Option[]) => {
    setOptions(next);
    setValue(next[0]?.value ?? "");
  };
  return (
    <form>
      <Select
        aria-label="Food"
        value={value}
        options={options}
        placeholder="Choose"
        onValueChange={(next) => {
          onValueChange(next);
          setValue(next);
        }}
      />
      <button type="button" onClick={() => pick(FRUIT)}>
        Fruit
      </button>
      <button type="button" onClick={() => pick(VEG)}>
        Veg
      </button>
    </form>
  );
}

describe("Select", () => {
  it("keeps a value set together with new options", async () => {
    const onValueChange = vi.fn();
    const { user } = renderWithProviders(<Harness onValueChange={onValueChange} />);
    const food = screen.getByRole("combobox", { name: "Food" });
    await user.click(screen.getByRole("button", { name: "Fruit" }));
    expect(food).toHaveTextContent("Apple");
    await user.click(screen.getByRole("button", { name: "Veg" }));
    expect(food).toHaveTextContent("Leek");
    // Radix's hidden <select> doesn't have the new option yet, so it reads "" back and reports that.
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it("still reports a real choice", async () => {
    const onValueChange = vi.fn();
    const { user } = renderWithProviders(<Harness onValueChange={onValueChange} />);
    await user.click(screen.getByRole("button", { name: "Fruit" }));
    await user.click(screen.getByRole("combobox", { name: "Food" }));
    await user.click(await screen.findByRole("option", { name: "Pear" }));
    expect(onValueChange).toHaveBeenCalledWith("pear");
    expect(screen.getByRole("combobox", { name: "Food" })).toHaveTextContent("Pear");
  });
});
