import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = process.env.CODELENS_API ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Same-origin in development too, so EventSource and fetch need no CORS.
    proxy: { "/api": api, "/webhooks": api },
  },
});
