<script setup lang="ts">
import { Clock3, History, LoaderCircle, Plus, RefreshCw, Route, WifiOff, X } from "@lucide/vue";

import ConversationPanel from "../components/ConversationPanel.vue";
import HistoryDrawer from "../components/HistoryDrawer.vue";
import InterruptPanel from "../components/InterruptPanel.vue";
import ItineraryPanel from "../components/ItineraryPanel.vue";
import ProgressPanel from "../components/ProgressPanel.vue";
import { useTravelThread } from "../composables/useTravelThread";

const travel = useTravelThread();

async function startNewTrip(): Promise<void> {
  travel.closeHistory();
  await travel.startNewTrip();
}
</script>

<template>
  <div class="workbench-shell">
    <a class="skip-link" href="#workbench-main">跳到主要内容</a>
    <header class="workbench-topbar">
      <div class="brand" aria-label="OpenZLTravel">
        <span class="brand-mark"><Route :size="20" /></span>
        <span><strong>OpenZLTravel</strong><small>LangGraph 旅行工作台</small></span>
      </div>
      <div class="topbar-actions">
        <span v-if="travel.threadId.value" class="thread-label" title="当前工作流 Thread ID">
          <Clock3 :size="14" />{{ travel.threadId.value.slice(0, 8) }}
        </span>
        <button
          class="secondary-button"
          type="button"
          :disabled="travel.running.value || travel.reconnecting.value"
          @click="startNewTrip"
        >
          <Plus :size="17" />新行程
        </button>
        <button class="secondary-button" type="button" @click="travel.openHistory">
          <History :size="17" />历史
        </button>
      </div>
    </header>

    <main id="workbench-main" class="workbench-main">
      <header class="workspace-heading">
        <div>
          <p class="section-kicker">单一工作流 · 真实事实边界</p>
          <h1>从需求到可执行行程</h1>
          <p>对话补齐需求，选择真实城市、车票和酒店；规划与审查完成后自动保存。</p>
        </div>
      </header>

      <div v-if="travel.error.value" class="global-alert" role="alert">
        <WifiOff v-if="travel.disconnected.value" :size="19" />
        <span>{{ travel.error.value }}</span>
        <button
          v-if="travel.disconnected.value"
          class="secondary-button"
          type="button"
          :disabled="travel.reconnecting.value"
          @click="travel.reconnect"
        >
          <LoaderCircle v-if="travel.reconnecting.value" class="spin" :size="16" />
          <RefreshCw v-else :size="16" />继续接收
        </button>
        <button v-else class="icon-button" type="button" aria-label="关闭错误提示" @click="travel.error.value = ''">
          <X :size="17" />
        </button>
      </div>

      <section v-if="travel.initializing.value" class="initializing-panel" role="status">
        <LoaderCircle class="spin" :size="22" />
        <div><strong>正在恢复旅行线程</strong><p>短期状态来自 LangGraph Checkpoint。</p></div>
      </section>

      <div v-else class="workbench-grid">
        <div class="conversation-column">
          <ConversationPanel
            :messages="travel.messages.value"
            :running="travel.running.value"
            :disabled="!travel.canSendMessage.value"
            :interrupt="travel.interrupt.value"
            @send="travel.submitMessage"
          />
          <InterruptPanel
            v-if="travel.interrupt.value"
            :interrupt="travel.interrupt.value"
            :selection="travel.state.value.selection"
            :busy="travel.running.value"
            @resume="travel.resume"
          />
        </div>

        <ProgressPanel
          :phase="travel.state.value.phase"
          :requirements="travel.state.value.requirements"
          :active-node="travel.activeNode.value"
          :warnings="travel.state.value.warnings"
          :errors="travel.state.value.errors"
        />
      </div>

      <ItineraryPanel
        v-if="travel.currentTrip.value"
        :trip="travel.currentTrip.value"
        :historical="Boolean(travel.viewedTrip.value && travel.viewedTrip.value.trip_id !== travel.state.value.trip_id)"
        @back="travel.returnToCurrentTrip"
      />
    </main>

    <HistoryDrawer
      :open="travel.historyOpen.value"
      :items="travel.history.value"
      :loading="travel.historyLoading.value"
      @close="travel.closeHistory"
      @refresh="travel.refreshHistory"
      @select="travel.viewHistoricalTrip"
      @delete="travel.deleteHistoricalTrip"
      @new-trip="startNewTrip"
    />
  </div>
</template>
