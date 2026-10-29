import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  images: {
    // Product photos the server made (server/photoStorage.py: Firebase
    // Storage download URLs; the Storage emulator's in localdev).
    remotePatterns: [
      { protocol: "https", hostname: "firebasestorage.googleapis.com", pathname: "/v0/b/**" },
      { protocol: "http", hostname: "127.0.0.1", port: "9199", pathname: "/v0/b/**" },
    ],
  },
};

export default nextConfig;
