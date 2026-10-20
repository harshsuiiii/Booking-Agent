import type { NextConfig } from "next";

/**
 * Where the API runs when deployed (e.g. https://booking-agent-api.onrender.com).
 * The browser only ever calls this site's /v1/...; Next forwards those requests to the
 * API, so they are same-origin and no CORS is involved. Read at build time: set it in the
 * hosting provider's env vars, then redeploy.
 */
const API_PROXY_URL = process.env.API_PROXY_URL?.replace(/\/+$/, "");

const nextConfig: NextConfig = {
  cacheComponents: true,
  // Dev-only "N" badge: keep it off the sidebar's bottom-left "Collapse sidebar" button.
  devIndicators: { position: "bottom-right" },
  async redirects() {
    return [{ source: "/agent", destination: "/assistant", permanent: false }];
  },
  async rewrites() {
    if (!API_PROXY_URL) return [];
    return [{ source: "/v1/:path*", destination: `${API_PROXY_URL}/v1/:path*` }];
  },
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
