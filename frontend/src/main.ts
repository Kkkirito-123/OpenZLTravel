// 前端入口只负责路由与应用挂载，页面逻辑保留在 pages 目录。
import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";

import App from "./App.vue";
import ChatPage from "./pages/ChatPage.vue";
import HistoryPage from "./pages/HistoryPage.vue";
import PlanPage from "./pages/PlanPage.vue";
import SessionPage from "./pages/SessionPage.vue";
import TripPage from "./pages/TripPage.vue";
import "./styles.css";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: ChatPage },
    { path: "/chat/:sessionId", component: ChatPage },
    { path: "/plan", component: PlanPage },
    { path: "/planning/:sessionId", component: SessionPage },
    { path: "/result/:tripId", component: TripPage },
    { path: "/history", component: HistoryPage },
  ],
});

createApp(App).use(router).mount("#app");
