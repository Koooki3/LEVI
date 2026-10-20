// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
import type { NextConfig } from "next";
import packageJson from "./package.json";

const nextConfig: NextConfig = {
  typescript: {
    ignoreBuildErrors: false,
  },
  eslint: {
    ignoreDuringBuilds: true,
  },
  transpilePackages: ["three"],
  // Avoid the 200-800ms cold-start cost of barrel-file imports.
  // react-icons re-exports thousands of icon components from /fa, etc.;
  // recharts and @huggingface/hub also have wide entry surfaces.
  experimental: {
    optimizePackageImports: ["react-icons", "recharts", "@huggingface/hub"],
  },
  generateBuildId: () => packageJson.version,
  // The robot-station page was /rig until 2026-10-20; old bookmarks and the
  // operator tutorial still point there. Temporary, so no browser keeps it.
  async redirects() {
    return [{ source: "/rig", destination: "/marvel0", permanent: false }];
  },
};

export default nextConfig;
