<script setup lang="ts">
import { ArrowRight, Building2, Check, LoaderCircle, MapPinned, Utensils } from "@lucide/vue";
import { ref } from "vue";

import type { DestinationSelectionInterrupt } from "../types";

defineProps<{ interrupt: DestinationSelectionInterrupt; busy: boolean }>();
const emit = defineEmits<{ select: [candidateId: string] }>();
const selectedId = ref("");
</script>

<template>
  <section class="interrupt-card destination-card" aria-labelledby="destination-title">
    <header>
      <span class="interrupt-icon"><MapPinned :size="20" /></span>
      <div>
        <p class="section-kicker">目的地推荐</p>
        <h3 id="destination-title">选择一个城市继续规划</h3>
        <p>候选来自地点目录的确定性评分，最多展示 5 个真实城市。</p>
      </div>
    </header>
    <p v-if="interrupt.error" class="interrupt-error" role="alert">{{ interrupt.error.message }}</p>

    <fieldset class="destination-grid">
      <legend class="sr-only">选择目的地城市</legend>
      <label
        v-for="candidate in interrupt.candidates"
        :key="candidate.candidate_id"
        :class="['destination-option', { selected: selectedId === candidate.candidate_id }]"
      >
        <input
          v-model="selectedId"
          type="radio"
          name="destination"
          :value="candidate.candidate_id"
          :disabled="busy"
        />
        <span class="destination-title-row">
          <strong>{{ candidate.city.name }}</strong>
          <span>{{ Math.round(candidate.score * 100) }} 分</span>
        </span>
        <span class="candidate-reasons">
          <small v-for="reason in candidate.reasons" :key="reason">{{ reason }}</small>
        </span>
        <span class="candidate-coverage">
          <small><MapPinned :size="13" />{{ candidate.attraction_count ?? 0 }} 景点</small>
          <small><Utensils :size="13" />{{ candidate.restaurant_count ?? 0 }} 餐饮</small>
          <small><Building2 :size="13" />{{ candidate.hotel_count ?? 0 }} 住宿</small>
        </span>
        <Check v-if="selectedId === candidate.candidate_id" class="option-check" :size="18" />
      </label>
    </fieldset>

    <footer class="interrupt-footer">
      <p>城市成本数据不可靠时，不使用预算参与排名。</p>
      <button
        class="primary-button"
        type="button"
        :disabled="busy || !selectedId"
        @click="emit('select', selectedId)"
      >
        <LoaderCircle v-if="busy" class="spin" :size="17" />
        <ArrowRight v-else :size="17" />
        使用这个城市
      </button>
    </footer>
  </section>
</template>
