import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // The generated ApiResponseSchema types `data` as an opaque value. Callers
  // use the generic ApiResponse<T> from '@/app/api/ApiResponse', which only
  // the generated client/app/api/ builds on it. `npm run lint` runs this;
  // `next build` does not.
  {
    files: ["**/*.{js,jsx,mjs,ts,tsx}"],
    ignores: ["app/api/**"],
    rules: {
      "no-restricted-imports": ["error", {
        patterns: [{
          group: [
            "**/schemas/ApiResponseSchema",
            "**/schemas/ApiResponseSchema.ts",
          ],
          message:
            "Import ApiResponse from '@/app/api/ApiResponse': its ApiResponse<T> narrows `data` to the route's model on `status === 200`.",
        }],
      }],
    },
  },
  // Generated files carry a blanket eslint-disable that is often unused:
  // never report it there (the generator, not a person, owns them).
  {
    files: ["app/schemas/**", "app/api/**", "lib/generated/**"],
    linterOptions: { reportUnusedDisableDirectives: "off" },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
