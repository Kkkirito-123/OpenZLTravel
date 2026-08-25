<script setup lang="ts">
import {
  Bot,
  History,
  LoaderCircle,
  Plus,
  RefreshCw,
  Route,
  Send,
  ShieldCheck,
  Sparkles,
  Wrench,
  X,
} from "@lucide/vue";
import { computed, nextTick, ref, watch } from "vue";

import ItineraryPanel from "../planning/ItineraryPanel.vue";
import RoutePreviewCard from "../planning/RoutePreviewCard.vue";
import HistoryDrawer from "../trips/HistoryDrawer.vue";
import AssistantCards from "./AssistantCards.vue";
import { useAssistantWorkspace } from "./useAssistantWorkspace";

const workspace = useAssistantWorkspace();
const input = ref("");
const messageList = ref<HTMLElement | null>(null);
const historical = computed(() => Boolean(
  workspace.displayedTrip.value
  && workspace.displayedTrip.value.trip_id !== workspace.planning.value.state.trip_id,
));
const requirements = computed(() => workspace.assistant.value.requirements);
const planningSteps = [
  ["validate_order", "验证工单"],
  ["build_itinerary", "生成每日行程"],
  ["build_routes", "查询最终路线"],
  ["calculate_budget", "计算预算"],
  ["validate_plan", "校验规划"],
  ["route_preview", "等待路线确认"],
  ["save_trip", "保存行程"],
] as const;

watch(
  () => [
    workspace.assistant.value.messages.length,
    workspace.pendingUserMessage.value,
    workspace.streamingReply.value,
    workspace.tools.value.length,
  ],
  async () => {
    await nextTick();
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: "smooth" });
  },
);

async function submit(): Promise<void> {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  await workspace.sendMessage(text);
}

function useExample(text: string): void {
  input.value = text;
  void submit();
}

function stepState(node: string): "active" | "done" | "idle" {
  if (workspace.activeNode.value === node) return "active";
  if (workspace.planning.value.state.phase === "completed") return "done";
  const current = planningSteps.findIndex(([key]) => key === workspace.activeNode.value);
  const target = planningSteps.findIndex(([key]) => key === node);
  return current > target ? "done" : "idle";
}
</script>

<template>
  <div class="assistant-shell">
    <header class="assistant-topbar">
      <div class="brand" aria-label="OpenZLTravel">
        <span class="brand-mark"><Sparkles :size="19" /></span>
        <span><strong>OpenZLTravel</strong><small>独立 AI 旅行助手</small></span>
      </div>
      <div class="topbar-actions">
        <button class="secondary-button" type="button" :disabled="workspace.running.value" @click="workspace.startNewTrip"><Plus :size="17" />新行程</button>
        <button class="secondary-button" type="button" @click="workspace.openHistory"><History :size="17" />历史</button>
      </div>
    </header>

    <main class="assistant-main">
      <section class="assistant-hero">
        <div><p class="section-kicker">一个入口 · 两个独立服务</p><h1>先聊清楚，再把工单交给规划图</h1><p>我会收集出发地、目的地、时间和预算，查询真实景点、车票、酒店与天气；选择完成后再启动确定性规划。</p></div>
        <div class="boundary-badges" aria-label="系统边界"><span><Bot :size="15" />Assistant 负责交流</span><span><Wrench :size="15" />工具事实可追踪</span><span><ShieldCheck :size="15" />签名工单交接</span></div>
      </section>

      <div v-if="workspace.error.value" class="global-alert" role="alert">
        <span>{{ workspace.error.value }}</span>
        <button v-if="workspace.disconnected.value" class="secondary-button" type="button" :disabled="workspace.reconnecting.value" @click="workspace.retry"><LoaderCircle v-if="workspace.reconnecting.value" class="spin" :size="16" /><RefreshCw v-else :size="16" />重新连接</button>
        <button v-else class="icon-button" type="button" aria-label="关闭错误" @click="workspace.error.value = ''"><X :size="17" /></button>
      </div>

      <section v-if="workspace.initializing.value" class="initializing-panel" role="status"><LoaderCircle class="spin" :size="22" /><div><strong>正在恢复当前浏览器会话</strong><p>恢复 Assistant 签名快照、规划 Run 和历史行程。</p></div></section>

      <div v-else class="assistant-workspace">
        <section class="conversation-workspace" aria-label="旅行助手对话">
          <div ref="messageList" class="message-list">
            <div v-if="!workspace.assistant.value.messages.length && !workspace.pendingUserMessage.value" class="empty-conversation">
              <span><Sparkles :size="24" /></span><h2>从一句旅行想法开始</h2><p>不需要填写表单。我会每次只追问一个关键问题。</p>
              <div class="example-grid"><button type="button" @click="useExample('从上海出发，国庆想去杭州玩三天，2个人预算5000元')">上海出发，杭州三天</button><button type="button" @click="useExample('从北京出发，推荐一个适合周末慢旅行的江南城市')">推荐江南周末城市</button></div>
            </div>

            <article v-for="message in workspace.assistant.value.messages" :key="message.message_id" :class="['chat-row', message.role]"><span class="chat-avatar"><Bot v-if="message.role === 'assistant'" :size="16" /><span v-else>我</span></span><p>{{ message.content }}</p></article>
            <article v-if="workspace.pendingUserMessage.value" class="chat-row user pending"><span class="chat-avatar">我</span><p>{{ workspace.pendingUserMessage.value }}</p></article>
            <article v-if="workspace.streamingReply.value" class="chat-row assistant"><span class="chat-avatar"><Bot :size="16" /></span><p>{{ workspace.streamingReply.value }}</p></article>

            <section v-if="workspace.isPlanning.value" class="planning-card">
              <header><Route :size="19" /><div><strong>Travel LangGraph 正在执行</strong><p>图只使用签名工单事实，并查询最终路线。</p></div></header>
              <ol class="planning-steps"><li v-for="step in planningSteps" :key="step[0]" :class="stepState(step[0])"><span>{{ stepState(step[0]) === 'done' ? '✓' : '' }}</span>{{ step[1] }}</li></ol>
              <div v-if="workspace.planning.value.state.draft" class="draft-preview"><article v-for="day in workspace.planning.value.state.draft.days" :key="day.day_index"><strong>第 {{ day.day_index }} 天 · {{ day.theme }}</strong><p>{{ day.activities.map((item) => workspace.planning.value.state.facts.catalog?.attractions.find((poi) => poi.id === item.poi_id)?.name || item.poi_id).join(' → ') || '当天留白' }}</p></article></div>
              <RoutePreviewCard v-if="workspace.planning.value.interrupt" :interrupt="workspace.planning.value.interrupt" :busy="workspace.running.value" @confirm="workspace.confirmPlanning" />
            </section>

            <ItineraryPanel v-if="workspace.displayedTrip.value" :trip="workspace.displayedTrip.value" :historical="historical" @back="workspace.returnToCurrentTrip" />
          </div>

          <form class="assistant-composer" @submit.prevent="submit"><textarea v-model="input" :disabled="!workspace.canSend.value" :placeholder="workspace.planning.value.interrupt ? '例如：把西湖放到第二天；或点击确认路线' : '告诉我你的旅行想法，或用自然语言修改当前选择…'" rows="2" @keydown.enter.exact.prevent="submit" /><button class="primary-button send-button" type="submit" :disabled="!input.trim() || !workspace.canSend.value"><LoaderCircle v-if="workspace.running.value" class="spin" :size="17" /><Send v-else :size="17" />发送</button></form>
        </section>

        <aside class="assistant-sidebar" aria-label="当前旅行资料与实时结果">
          <section class="session-summary">
            <header><p class="section-kicker">当前会话</p><h2>旅行资料</h2></header>
            <dl><div><dt>出发地</dt><dd>{{ requirements.origin || '待确认' }}</dd></div><div><dt>目的地</dt><dd>{{ requirements.destination || requirements.region || '待确认' }}</dd></div><div><dt>日期</dt><dd>{{ requirements.start_date || '待确认' }}<template v-if="requirements.end_date"> — {{ requirements.end_date }}</template></dd></div><div><dt>人数</dt><dd>{{ requirements.travelers || 1 }} 人</dd></div><div><dt>预算</dt><dd>{{ requirements.budget == null ? '待确认' : `${requirements.budget} 元` }}</dd></div><div><dt>节奏</dt><dd>{{ requirements.pace || '适中' }}</dd></div></dl>
            <div class="session-status"><span :class="workspace.assistant.value.status" />Assistant：{{ workspace.assistant.value.status }}</div><p>公开快照与签名 Token 仅保存在当前浏览器会话；Graph 不接收前端自造事实。</p>
          </section>

          <section v-if="workspace.tools.value.length" class="tool-trace" aria-label="工具调用状态"><header><Wrench :size="15" /><strong>工具活动</strong></header><ul><li v-for="tool in workspace.tools.value" :key="tool.name"><LoaderCircle v-if="tool.status === 'running'" class="spin" :size="14" /><span v-else class="tool-done">✓</span><span>{{ tool.name }}</span><small>{{ tool.artifact || tool.status }}</small></li></ul></section>

          <AssistantCards v-if="!workspace.displayedTrip.value && !workspace.isPlanning.value" :snapshot="workspace.assistant.value" :busy="workspace.running.value" @select="workspace.select" />
        </aside>
      </div>
    </main>

    <HistoryDrawer :open="workspace.historyOpen.value" :items="workspace.history.value" :loading="workspace.historyLoading.value" @close="workspace.historyOpen.value = false" @refresh="workspace.refreshHistory" @select="workspace.viewHistoricalTrip" @delete="workspace.deleteHistoricalTrip" @new-trip="workspace.startNewTrip" />
  </div>
</template>
