import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["node_modules", "dist", "test-results", "playwright-report", "src/lib/api.gen.ts"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      // The tower is read-only (D35): no state-changing HTTP from the client.
      "no-restricted-syntax": [
        "error",
        {
          selector: "Property[key.name='method'][value.value=/^(POST|PUT|PATCH|DELETE)$/i]",
          message: "The tower is read-only: GET only.",
        },
      ],
    },
  },
);
