import { createContext, useContext } from "react";

/** What a Field gives its control: the id its <label> points at, plus error/hint wiring. */
export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

export const FieldContext = createContext<FieldControlProps | null>(null);

/** Inside a Field: the props that tie this control to the field's label, error and hint. */
export function useFieldControl(): FieldControlProps | null {
  return useContext(FieldContext);
}
