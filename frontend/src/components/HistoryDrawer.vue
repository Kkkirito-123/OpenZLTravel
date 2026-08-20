<script setup lang="ts">
import { CalendarDays, LoaderCircle, Plus, Trash2, X } from "@lucide/vue";
import { nextTick, onBeforeUnmount, ref, watch } from "vue";

import type { TripSummary } from "../types";

const props = defineProps<{
  open: boolean;
  items: TripSummary[];
  loading: boolean;
}>();
const emit = defineEmits<{
  close: [];
  refresh: [];
  select: [tripId: string];
  delete: [tripId: string];
  newTrip: [];
}>();

const closeButton = ref<HTMLButtonElement | null>(null);
const confirmingId = ref<string | null>(null);

watch(
  () => props.open,
  async (open) => {
    if (!open) {
      confirmingId.value = null;
      window.removeEventListener("keydown", handleKeydown);
      return;
    }
    window.addEventListener("keydown", handleKeydown);
    await nextTick();
    closeButton.value?.focus();
  },
  { immediate: true },
);

onBeforeUnmount(() => window.removeEventListener("keydown", handleKeydown));

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") emit("close");
}

function askDelete(event: Event, tripId: string): void {
  event.stopPropagation();
  confirmingId.value = tripId;
}

function confirmDelete(event: Event, tripId: string): void {
  event.stopPropagation();
  confirmingId.value = null;
  emit("delete", tripId);
}

function dateLabel(value?: string): string {
  if (!value) return "日期未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "short", day: "numeric" }).format(date);
}
</script>

<template>
  <Transition name="drawer">
    <div v-if="open" class="drawer-backdrop" @click.self="emit('close')">
      <aside
        class="history-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="history-title"
      >
        <header class="drawer-heading">
          <div><p class="section-kicker">已保存</p><h2 id="history-title">历史行程</h2></div>
          <button ref="closeButton" class="icon-button" type="button" aria-label="关闭历史行程" @click="emit('close')">
            <X :size="20" />
          </button>
        </header>

        <button class="secondary-button drawer-new-button" type="button" @click="emit('newTrip')">
          <Plus :size="17" />开始新行程
        </button>

        <div v-if="loading" class="drawer-loading" role="status">
          <LoaderCircle class="spin" :size="20" />正在读取历史行程…
        </div>
        <div v-else-if="!items.length" class="drawer-empty">
          <CalendarDays :size="28" />
          <strong>还没有已保存行程</strong>
          <p>规划完成并通过校验后，会自动出现在这里。</p>
          <button class="text-button" type="button" @click="emit('refresh')">重新加载</button>
        </div>
        <ul v-else class="history-list">
          <li v-for="item in items" :key="item.trip_id">
            <button class="history-main" type="button" @click="emit('select', item.trip_id)">
              <strong>{{ item.destination }}</strong>
              <span>{{ item.start_date || "日期待确认" }}<template v-if="item.end_date"> — {{ item.end_date }}</template></span>
              <p v-if="item.summary">{{ item.summary }}</p>
              <small>保存于 {{ dateLabel(item.created_at) }}</small>
            </button>
            <div v-if="confirmingId === item.trip_id" class="delete-confirm" role="alert">
              <span>确认删除？</span>
              <button type="button" @click="confirmingId = null">取消</button>
              <button class="danger-text" type="button" @click="confirmDelete($event, item.trip_id)">删除</button>
            </div>
            <button
              v-else
              class="icon-button history-delete"
              type="button"
              :aria-label="`删除 ${item.destination} 行程`"
              @click="askDelete($event, item.trip_id)"
            >
              <Trash2 :size="17" />
            </button>
          </li>
        </ul>
      </aside>
    </div>
  </Transition>
</template>
