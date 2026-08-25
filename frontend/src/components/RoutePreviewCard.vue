<script setup lang="ts">
import { CheckCircle2, LoaderCircle, Route } from "@lucide/vue";
import { ref } from "vue";

import type { RoutePreviewInterrupt } from "../types";

const props = defineProps<{ interrupt: RoutePreviewInterrupt; busy: boolean }>();
const emit = defineEmits<{ confirm: [allowOverBudget: boolean] }>();
const allowOverBudget = ref(false);
</script>

<template>
  <section class="interrupt-card route-preview-card" aria-labelledby="route-preview-title">
    <header>
      <span class="interrupt-icon"><Route :size="20" /></span>
      <div>
        <p class="section-kicker">路线预览</p>
        <h3 id="route-preview-title">确认每天先去哪、再去哪</h3>
        <p>{{ interrupt.question }}</p>
      </div>
    </header>
    <p v-if="interrupt.error" class="interrupt-error" role="alert">{{ interrupt.error.message }}</p>
    <p class="chat-clarification-hint">想调整可直接回复，例如“把兵马俑放第一天”或“第二天少安排一个”。</p>
    <label v-if="props.interrupt.is_over_budget" class="budget-warning-confirm">
      <input v-model="allowOverBudget" type="checkbox" />
      我知道最终估算超过预算，仍生成报告
    </label>
    <footer class="interrupt-footer">
      <button
        class="primary-button"
        type="button"
        :disabled="busy || (props.interrupt.is_over_budget === true && !allowOverBudget)"
        @click="emit('confirm', allowOverBudget)"
      >
        <LoaderCircle v-if="busy" class="spin" :size="17" />
        <CheckCircle2 v-else :size="17" />
        按这个走，生成最终报告
      </button>
    </footer>
  </section>
</template>
