import type { NextConfig } from "next";

// localdev's product photos come from the Storage emulator on this machine
// (server/photoStorage.py): only there may the image optimizer fetch a local
// address. A deployed build allows Firebase Storage alone.
const localdev = process.env.NEXT_PUBLIC_ENVIRONMENT === "localdev";
const emulatorPhotos = { protocol: "http", hostname: "127.0.0.1", port: "9199", pathname: "/v0/b/**" } as const;

const nextConfig: NextConfig = {
  images: {
    // Every image goes through next/image (eslint.config.mjs enforces it).
    remotePatterns: [
      { protocol: "https", hostname: "firebasestorage.googleapis.com", pathname: "/v0/b/**" },
      ...(localdev ? [emulatorPhotos] : []),
    ],
    dangerouslyAllowLocalIP: localdev,
  },
};

export default nextConfig;
