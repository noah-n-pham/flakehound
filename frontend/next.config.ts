import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Next writes AGENTS.md and CLAUDE.md into this folder on every dev start.
  // This repository is public and the build scaffolding is not part of the
  // product — the same reason docs/ and .cursor/ are untracked.
  agentRules: false,
};

export default nextConfig;
