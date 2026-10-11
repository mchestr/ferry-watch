import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/", "coverage/"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked, ...tseslint.configs.stylisticTypeChecked],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-hooks/exhaustive-deps": "error",
      "react-refresh/only-export-components": ["error", { allowConstantExport: true }],
      // `${count} trips` reads fine; booleans, nullish values and objects are still rejected.
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
      // `onClick={() => setOpen(false)}` is idiomatic React.
      "@typescript-eslint/no-confusing-void-expression": ["error", { ignoreArrowShorthand: true }],
    },
  },
  {
    files: ["**/*.js"],
    extends: [js.configs.recommended, tseslint.configs.disableTypeChecked],
  },
);
