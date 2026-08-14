<!-- 多轮旅行助手：聊天只收集需求，真实事实仍由现有规划会话查询。 -->
<script setup lang="ts">
import {
  ArrowRight,
  Brain,
  ClipboardList,
  MessageCircle,
  RefreshCw,
  Send,
  Trash2,
} from "@lucide/vue";
import { computed, nextTick, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import {
  createAssistantSession,
  deleteAssistantMemory,
  errorMessage,
  getAssistantSession,
  listAssistantMemories,
  sendAssistantMessage,
} from "../api";
import type {
  AssistantMessageRequest,
  AssistantSessionView,
  MemorySlotName,
  TravelDialogueSlots,
} from "../types";

const route = useRoute();
const router = useRouter();
const session = ref<AssistantSessionView | null>(null);
const input = ref("");
const initializing = ref(false);
const sending = ref(false);
const error = ref("");
const failedRequest = ref<AssistantMessageRequest | null>(null);
const pendingContent = ref("");
const messageLog = ref<HTMLElement | null>(null);
const deletingMemory = ref<MemorySlotName | null>(null);

const state = computed(() => session.value?.state ?? null);
const statusText = computed(() => {
  const labels = {
    collecting: "正在收集需求",
    recommendation_ready: "推荐需求已整理",
    planning_started: "规划数据查询中",
    closed: "会话已关闭",
  };
  return state.value ? labels[state.value.status] : "正在建立会话";
});
const requirementItems = computed(() => {
  if (!state.value) return [];
  const slots = state.value.slots;
  const known = state.value.slot_metadata;
  const items: { key: string; label: string; value: string }[] = [];
  addRequirement(items, known, "origin", "出发地", slots.origin);
  addRequirement(
    items,
    known,
    "destination_city",
    "目的城市",
    slots.destination_city,
  );
  addRequirement(
    items,
    known,
    "destination_region",
    "目的地区",
    slots.destination_region,
  );
  addRequirement(items, known, "start_date", "出发日期", slots.start_date);
  addRequirement(items, known, "end_date", "结束日期", slots.end_date);
  addRequirement(items, known, "days", "行程天数", slots.days, " 天");
  addRequirement(items, known, "budget", "预算", slots.budget, " 元");
  addRequirement(items, known, "travelers", "人数", slots.travelers, " 人");
  addRequirement(
    items,
    known,
    "preferences",
    "偏好",
    slots.preferences.join("、"),
  );
  addRequirement(
    items,
    known,
    "distance_preference",
    "距离倾向",
    slots.distance_preference === "near"
      ? "不希望太远"
      : slots.distance_preference === "far"
        ? "可以去远一些"
        : null,
  );
  return items;
});
const pendingLabels = computed(() =>
  (state.value?.pending_slots ?? []).map((item) => SLOT_LABELS[item] ?? item),
);
const memoryItems = computed(() =>
  (session.value?.memories ?? []).map((memory) => ({
    ...memory,
    label: MEMORY_LABELS[memory.key],
    display: Array.isArray(memory.value) ? memory.value.join("、") : memory.value,
  })),
);

watch(
  () => route.params.sessionId,
  async (value) => {
    const sessionId = typeof value === "string" ? value : null;
    if (sessionId && session.value?.state.session_id === sessionId) return;
    if (sessionId) {
      await restoreSession(sessionId);
    } else if (!session.value) {
      await startSession();
    }
  },
  { immediate: true },
);

async function startSession(): Promise<void> {
  initializing.value = true;
  error.value = "";
  try {
    session.value = await createAssistantSession();
    await router.replace(`/chat/${session.value.state.session_id}`);
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    initializing.value = false;
  }
}

async function restoreSession(sessionId: string): Promise<void> {
  initializing.value = true;
  error.value = "";
  try {
    session.value = await getAssistantSession(sessionId);
    await scrollToLatest();
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    initializing.value = false;
  }
}

async function sendMessage(retry?: AssistantMessageRequest): Promise<void> {
  const content = retry?.content ?? input.value.trim();
  if (!session.value || sending.value || !content) return;
  const message = retry ?? { message_id: newMessageId(), content };
  sending.value = true;
  error.value = "";
  failedRequest.value = null;
  pendingContent.value = content;
  if (!retry) input.value = "";
  await scrollToLatest();
  try {
    const response = await sendAssistantMessage(session.value.state.session_id, message);
    const sequence = (session.value.turns.at(-1)?.sequence ?? 0) + 1;
    session.value = {
      state: response.state,
      skill: response.skill,
      memories: session.value.memories,
      turns: [
        ...session.value.turns,
        {
          sequence,
          user_content: content,
          assistant_content: response.reply,
          created_at: new Date().toISOString(),
        },
      ],
    };
    session.value.memories = await listAssistantMemories().catch(
      () => session.value?.memories ?? [],
    );
    pendingContent.value = "";
  } catch (requestError) {
    error.value = errorMessage(requestError);
    failedRequest.value = message;
  } finally {
    sending.value = false;
    await scrollToLatest();
  }
}

async function removeMemory(key: MemorySlotName): Promise<void> {
  if (!session.value || deletingMemory.value) return;
  deletingMemory.value = key;
  error.value = "";
  try {
    await deleteAssistantMemory(key);
    session.value.memories = session.value.memories.filter((item) => item.key !== key);
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    deletingMemory.value = null;
  }
}

async function scrollToLatest(): Promise<void> {
  await nextTick();
  const target = messageLog.value;
  if (!target) return;
  if (typeof target.scrollTo === "function") {
    target.scrollTo({ top: target.scrollHeight, behavior: "smooth" });
  } else {
    target.scrollTop = target.scrollHeight;
  }
}

function newMessageId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

function addRequirement(
  target: { key: string; label: string; value: string }[],
  metadata: Record<string, unknown>,
  key: keyof TravelDialogueSlots,
  label: string,
  value: string | number | null,
  suffix = "",
): void {
  if (!(key in metadata) || value === null || value === "") return;
  target.push({ key, label, value: `${value}${suffix}` });
}

const SLOT_LABELS: Record<string, string> = {
  origin: "出发地",
  destination_region: "目的地区",
  destination_city: "目的城市",
  start_date: "出发日期",
  end_date: "结束日期",
  days: "行程天数",
  budget: "预算",
  preferences: "旅行偏好",
};

const MEMORY_LABELS: Record<MemorySlotName, string> = {
  origin: "常用出发地",
  preferences: "旅行偏好",
  dietary_preferences: "饮食偏好",
  pace: "旅行节奏",
  hotel_level: "住宿档次",
  transport_mode: "市内交通",
};
</script>

<template>
  <section class="chat-page">
    <header class="chat-heading">
      <div>
        <p class="section-kicker">专属旅行助手</p>
        <h1>从想法开始规划</h1>
      </div>
      <span class="assistant-status"><span aria-hidden="true" />{{ statusText }}</span>
    </header>

    <div v-if="initializing && !session" class="chat-loading" aria-live="polite">
      <RefreshCw :size="20" class="spin" />正在恢复会话…
    </div>

    <div v-else-if="!session" class="chat-load-error">
      <p class="error-message" role="alert">{{ error }}</p>
      <button class="secondary-button" type="button" @click="startSession">
        <RefreshCw :size="17" />重试
      </button>
    </div>

    <div v-else class="chat-layout">
      <section class="conversation-panel" aria-label="旅行对话">
        <div ref="messageLog" class="chat-messages" role="log" aria-live="polite">
          <article class="message-row assistant-message">
            <span class="message-avatar" aria-hidden="true"><MessageCircle :size="18" /></span>
            <div class="message-bubble">
              你可以直接告诉我旅行想法；信息不足时，我会逐项确认。
            </div>
          </article>

          <template v-for="turn in session.turns" :key="turn.sequence">
            <article class="message-row user-message">
              <div class="message-bubble">{{ turn.user_content }}</div>
            </article>
            <article class="message-row assistant-message">
              <span class="message-avatar" aria-hidden="true"><MessageCircle :size="18" /></span>
              <div class="message-bubble">{{ turn.assistant_content }}</div>
            </article>
          </template>

          <article v-if="pendingContent" class="message-row user-message pending-message">
            <div class="message-bubble">{{ pendingContent }}</div>
          </article>
          <article v-if="sending" class="message-row assistant-message" aria-label="助手处理中">
            <span class="message-avatar" aria-hidden="true"><MessageCircle :size="18" /></span>
            <div class="message-bubble typing-indicator"><i /><i /><i /></div>
          </article>
        </div>

        <div v-if="error && session" class="chat-error" role="alert">
          <span>{{ error }}</span>
          <button
            v-if="failedRequest"
            type="button"
            :disabled="sending"
            @click="sendMessage(failedRequest)"
          >
            <RefreshCw :size="16" />重试
          </button>
        </div>

        <form class="chat-composer" @submit.prevent="sendMessage()">
          <label class="sr-only" for="assistant-message">输入旅行需求</label>
          <textarea
            id="assistant-message"
            v-model="input"
            rows="2"
            maxlength="1000"
            placeholder="例如：我想从杭州去上海玩三天，预算两千元"
            :disabled="sending || state?.status === 'closed'"
            @keydown.enter.exact.prevent="sendMessage()"
          />
          <div class="composer-actions">
            <span>{{ input.length }} / 1000</span>
            <button
              class="primary-button"
              type="submit"
              :disabled="sending || !input.trim() || state?.status === 'closed'"
            >
              <Send :size="18" />{{ sending ? "识别中…" : "发送" }}
            </button>
          </div>
        </form>
      </section>

      <aside class="requirements-panel" aria-labelledby="requirements-title">
        <header>
          <ClipboardList :size="19" />
          <h2 id="requirements-title">当前需求</h2>
        </header>
        <dl v-if="requirementItems.length" class="requirement-list">
          <div v-for="item in requirementItems" :key="item.key">
            <dt>{{ item.label }}</dt>
            <dd>{{ item.value }}</dd>
          </div>
        </dl>
        <p v-else class="requirements-empty">需求会随对话逐步整理。</p>

        <div v-if="pendingLabels.length" class="pending-summary">
          <span>还需确认</span>
          <p>{{ pendingLabels.join("、") }}</p>
        </div>

        <section v-if="session.skill" class="assistant-skill" aria-label="当前技能">
          <span>当前 Skill</span>
          <strong>{{ session.skill.title }}</strong>
          <p>{{ session.skill.description }}</p>
        </section>

        <section class="assistant-memory" aria-labelledby="memory-title">
          <header>
            <Brain :size="17" />
            <h3 id="memory-title">长期偏好</h3>
          </header>
          <ul v-if="memoryItems.length">
            <li v-for="memory in memoryItems" :key="memory.key">
              <div>
                <span>{{ memory.label }}</span>
                <strong>{{ memory.display }}</strong>
              </div>
              <button
                type="button"
                :disabled="deletingMemory === memory.key"
                :title="`删除${memory.label}`"
                :aria-label="`删除${memory.label}`"
                @click="removeMemory(memory.key)"
              >
                <Trash2 :size="16" />
              </button>
            </li>
          </ul>
          <p v-else>暂无长期偏好</p>
        </section>

        <p class="token-usage">
          本会话模型用量
          <strong>{{ state?.token_usage.total_tokens ?? 0 }}</strong>
          Token
        </p>

        <RouterLink
          v-if="state?.planning_session_id"
          class="planning-link"
          :to="`/planning/${state.planning_session_id}`"
        >
          查看车票与酒店<ArrowRight :size="17" />
        </RouterLink>
        <p v-else-if="state?.status === 'recommendation_ready'" class="ready-note">
          推荐需求已保存。当前版本不会生成缺少数据支持的城市推荐。
        </p>
      </aside>
    </div>
  </section>
</template>
