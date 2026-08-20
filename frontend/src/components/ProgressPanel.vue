<script setup lang="ts">
import {
  Check,
  Circle,
  CircleAlert,
  CloudSun,
  Hotel,
  LoaderCircle,
  MapPinned,
  TrainFront,
} from "@lucide/vue";
import { computed } from "vue";

import type { GraphNotice, TravelPhase, TravelRequirements } from "../types";

const props = defineProps<{
  phase: TravelPhase;
  requirements: TravelRequirements;
  activeNode: string | null;
  warnings: GraphNotice[];
  errors: GraphNotice[];
}>();

const steps = [
  { phase: "collecting", label: "确认需求" },
  { phase: "discovering", label: "查询事实" },
  { phase: "awaiting_selection", label: "选择方案" },
  { phase: "planning", label: "编排行程" },
  { phase: "reviewing", label: "校验结果" },
  { phase: "completed", label: "完成保存" },
] as const;

const currentIndex = computed(() => Math.max(
  0,
  steps.findIndex((step) => step.phase === props.phase),
));
const factsInProgress = computed(() => {
  const value = props.activeNode?.toLowerCase() ?? "";
  if (value.includes("rail")) return { label: "正在查询铁路", icon: TrainFront };
  if (value.includes("hotel")) return { label: "正在查询住宿", icon: Hotel };
  if (value.includes("weather")) return { label: "正在查询天气", icon: CloudSun };
  if (value.includes("catalog") || value.includes("destination")) {
    return { label: "正在准备地点目录", icon: MapPinned };
  }
  return null;
});
const requirementRows = computed(() => [
  ["出发", props.requirements.origin],
  ["目的地", props.requirements.destination ?? props.requirements.region],
  ["日期", dateRange(props.requirements)],
  ["人数", props.requirements.travelers ? `${props.requirements.travelers} 人` : null],
  ["预算", props.requirements.budget ? money(props.requirements.budget) : null],
].filter((row): row is [string, string] => Boolean(row[1])));

function statusAt(index: number): "done" | "active" | "pending" | "error" {
  if (props.phase === "failed" || props.phase === "cancelled") {
    return index === currentIndex.value ? "error" : index < currentIndex.value ? "done" : "pending";
  }
  if (index < currentIndex.value) return "done";
  if (index === currentIndex.value) return props.phase === "completed" ? "done" : "active";
  return "pending";
}

function dateRange(requirements: TravelRequirements): string | null {
  if (!requirements.start_date) return null;
  return requirements.end_date
    ? `${requirements.start_date} — ${requirements.end_date}`
    : requirements.start_date;
}

function money(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    maximumFractionDigits: 0,
  }).format(value);
}
</script>

<template>
  <aside class="progress-panel" aria-labelledby="progress-title">
    <header class="panel-heading compact-heading">
      <div>
        <p class="section-kicker">执行状态</p>
        <h2 id="progress-title">旅行图进度</h2>
      </div>
    </header>

    <ol class="phase-list">
      <li v-for="(step, index) in steps" :key="step.phase" :class="statusAt(index)">
        <span class="phase-marker">
          <Check v-if="statusAt(index) === 'done'" :size="14" />
          <LoaderCircle v-else-if="statusAt(index) === 'active'" class="spin" :size="14" />
          <CircleAlert v-else-if="statusAt(index) === 'error'" :size="14" />
          <Circle v-else :size="12" />
        </span>
        <span>{{ step.label }}</span>
      </li>
    </ol>

    <div v-if="factsInProgress" class="provider-progress" role="status">
      <component :is="factsInProgress.icon" :size="17" />
      <span>{{ factsInProgress.label }}</span>
    </div>

    <section v-if="requirementRows.length" class="requirement-summary" aria-labelledby="summary-title">
      <h3 id="summary-title">已确认需求</h3>
      <dl>
        <div v-for="row in requirementRows" :key="row[0]">
          <dt>{{ row[0] }}</dt>
          <dd>{{ row[1] }}</dd>
        </div>
      </dl>
    </section>

    <section v-if="warnings.length" class="notice-list warning-list" aria-label="降级与提醒">
      <article v-for="warning in warnings" :key="`${warning.code}-${warning.node}`">
        <CircleAlert :size="16" />
        <span>{{ warning.message }}</span>
      </article>
    </section>
    <section v-if="errors.length" class="notice-list error-list" aria-label="运行错误">
      <article v-for="notice in errors" :key="`${notice.code}-${notice.node}`">
        <CircleAlert :size="16" />
        <span>{{ notice.message }}</span>
      </article>
    </section>
  </aside>
</template>
