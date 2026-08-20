<script setup lang="ts">
import { Bot, CornerDownLeft, LoaderCircle, Send, UserRound } from "@lucide/vue";
import { computed, nextTick, ref, watch } from "vue";

import type { ConversationMessage } from "../composables/useTravelThread";
import type { TravelInterrupt } from "../types";

const props = defineProps<{
  messages: ConversationMessage[];
  running: boolean;
  disabled: boolean;
  interrupt?: TravelInterrupt | null;
}>();

const emit = defineEmits<{ send: [content: string] }>();
const draft = ref("");
const messageList = ref<HTMLElement | null>(null);
const examples = [
  "我从上海出发，国庆去杭州玩 3 天，喜欢人文和美食",
  "推荐一个适合周末慢节奏旅行的江南城市",
];
const assistantPrompt = computed(() => {
  const current = props.interrupt;
  if (!current) return "";
  if (current.kind === "clarification") return current.question;
  if (current.kind === "destination_selection") {
    return `我从真实地点目录整理了 ${current.candidates.length} 个城市候选。可以点击下方候选，也可以直接回复城市名称或“第 1 个”。`;
  }
  return "真实车票、酒店和天气事实已经查到。可以点击下方候选，也可以在聊天中回复“酒店选第 1 个，去程自行安排”。";
});

watch(
  () => [props.messages.length, props.running],
  async () => {
    await nextTick();
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: "smooth" });
  },
);

function submit(): void {
  const content = draft.value.trim();
  if (!content || props.disabled) return;
  draft.value = "";
  emit("send", content);
}

function useExample(content: string): void {
  if (props.disabled) return;
  emit("send", content);
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  submit();
}
</script>

<template>
  <section class="conversation-panel" aria-labelledby="conversation-title">
    <header class="panel-heading">
      <div>
        <p class="section-kicker">旅行对话</p>
        <h2 id="conversation-title">告诉我这次想怎么走</h2>
      </div>
      <span :class="['live-status', { busy: running }]" role="status">
        <LoaderCircle v-if="running" class="spin" :size="15" />
        <span v-else class="status-dot" aria-hidden="true" />
        {{ running ? "正在规划" : interrupt ? "可以用文字确认" : "可以继续输入" }}
      </span>
    </header>

    <div ref="messageList" class="message-list" aria-live="polite">
      <div v-if="!messages.length" class="conversation-empty">
        <Bot :size="28" />
        <div>
          <strong>从一句自然语言开始</strong>
          <p>出发地、目的地或地区、日期越明确，越快进入真实车票与酒店选择。</p>
        </div>
        <div class="example-list" aria-label="输入示例">
          <button
            v-for="example in examples"
            :key="example"
            type="button"
            :disabled="disabled"
            @click="useExample(example)"
          >
            {{ example }}
          </button>
        </div>
      </div>

      <article
        v-for="message in messages"
        :key="message.id"
        :class="['message-row', `${message.role}-message`, { pending: message.pending }]"
      >
        <span v-if="message.role === 'assistant'" class="message-avatar" aria-hidden="true">
          <Bot :size="18" />
        </span>
        <div class="message-bubble">
          <p>{{ message.text }}</p>
          <small v-if="message.pending">正在发送…</small>
        </div>
        <span v-if="message.role === 'user'" class="message-avatar user-avatar" aria-hidden="true">
          <UserRound :size="18" />
        </span>
      </article>

      <article v-if="assistantPrompt" class="message-row assistant-message assistant-prompt">
        <span class="message-avatar" aria-hidden="true"><Bot :size="18" /></span>
        <div class="message-bubble">
          <p>{{ assistantPrompt }}</p>
          <small>也可以使用下方结构化候选卡片确认</small>
        </div>
      </article>

      <article v-if="running" class="message-row assistant-message processing-message">
        <span class="message-avatar" aria-hidden="true"><Bot :size="18" /></span>
        <div class="message-bubble typing-bubble">
          <span /><span /><span />
          <span class="sr-only">正在处理旅行信息</span>
        </div>
      </article>
    </div>

    <form class="composer" @submit.prevent="submit">
      <label for="travel-message">旅行需求</label>
      <div class="composer-control">
        <textarea
          id="travel-message"
          v-model="draft"
          rows="2"
          :disabled="disabled"
          placeholder="例如：北京出发，10 月去苏州三天，预算 5000 元…"
          @keydown="handleKeydown"
        />
        <button
          class="primary-button send-button"
          type="submit"
          :disabled="disabled || !draft.trim()"
          aria-label="发送旅行需求"
        >
          <LoaderCircle v-if="running" class="spin" :size="18" />
          <Send v-else :size="18" />
        </button>
      </div>
      <p><CornerDownLeft :size="13" />Enter 发送，Shift + Enter 换行</p>
    </form>
  </section>
</template>
