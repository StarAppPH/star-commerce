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
  // Every image is next/image (optimized, resized and cached by Vercel):
  // never a raw <img>, Base UI's avatar <img>, a CSS background image, or an
  // unoptimized next/image without a stated reason.
  {
    files: ["**/*.{js,jsx,mjs,ts,tsx}"],
    rules: {
      "@next/next/no-img-element": "error",
      "no-restricted-syntax": ["error",
        {
          selector: "JSXAttribute[name.name='unoptimized']",
          message: "next/image optimizes every image: drop `unoptimized`. Only an image no server can fetch (a blob: preview) may keep it, with an eslint-disable comment saying why.",
        },
        {
          selector: "JSXMemberExpression[object.name='AvatarPrimitive'][property.name='Image']",
          message: "Use AvatarImage from '@/components/ui/avatar': it renders next/image.",
        },
        {
          selector: "Property[key.name='backgroundImage']",
          message: "Render images with next/image, not a CSS background image.",
        },
        {
          selector: "Literal[value=/bg-\\[url\\(/], TemplateElement[value.raw=/bg-\\[url\\(/]",
          message: "Render images with next/image, not a Tailwind url() background.",
        },
      ],
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
