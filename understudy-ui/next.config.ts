import type { NextConfig } from "next";

/**
 * UNDERSTUDY_BACKEND_INTERNAL_URL (build time) - when set (Cloud Run single-container image: http://127.0.0.1:8000),
 * the web UI proxies the backend's API and the demo ERP portals, so the whole app is served from ONE public origin.
 * Unset (local development, docker-compose) the browser calls the backend directly (http://localhost:8000).
 */
const backend = (process.env.UNDERSTUDY_BACKEND_INTERNAL_URL || "").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  experimental: {
    // Recordings are uploaded through the proxy: allow the backend's own limit (250 MB; Cloud Run itself caps
    // HTTP/1 requests at 32 MiB) instead of Next's 10 MB default, and give slow uploads time.
    proxyClientMaxBodySize: "260mb",
    proxyTimeout: 300_000,
  },
  async rewrites() {
    if (!backend) return [];
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      { source: "/portal", destination: `${backend}/portal` },
      { source: "/portal1", destination: `${backend}/portal1` },
      { source: "/portal2", destination: `${backend}/portal2` },
      { source: "/portal3", destination: `${backend}/portal3` },
      { source: "/erp-static/:path*", destination: `${backend}/erp-static/:path*` },
      { source: "/healthz", destination: `${backend}/api/health` },
    ];
  },
};

export default nextConfig;
