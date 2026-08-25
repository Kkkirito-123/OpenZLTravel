import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const agentServer = env.VITE_AGENT_SERVER_TARGET || "http://127.0.0.1:2024";
  const assistantServer = env.VITE_ASSISTANT_SERVER_TARGET || "http://127.0.0.1:2030";
  return {
    plugins: [vue()],
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    server: {
      port: 5173,
      proxy: {
        "/api/assistant": { target: assistantServer, changeOrigin: false },
        "/api": { target: agentServer, changeOrigin: false },
        "/threads": { target: agentServer, changeOrigin: false },
        "/runs": { target: agentServer, changeOrigin: false },
        "/assistants": { target: agentServer, changeOrigin: false },
      },
    },
    test: {
      environment: "jsdom",
    },
  };
});
