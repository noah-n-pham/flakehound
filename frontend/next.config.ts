import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Suppress the assistant instruction files Next writes into this folder on every
  // dev start; they are editor tooling rather than part of the application.
  agentRules: false,
};

export default nextConfig;
