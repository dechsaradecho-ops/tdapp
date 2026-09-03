/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Static Export: build outputs plain static files into `out/`.
  // Deploy the `out/` directory with any static host (Render Static Site, etc.).
  output: "export",
};

module.exports = nextConfig;
