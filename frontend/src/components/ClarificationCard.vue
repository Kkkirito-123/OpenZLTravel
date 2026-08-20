<script setup lang="ts">
import { ArrowRight, HelpCircle, LoaderCircle } from "@lucide/vue";
import { computed, reactive, ref } from "vue";

import type { ClarificationInterrupt, TravelRequirements } from "../types";

const props = defineProps<{ interrupt: ClarificationInterrupt; busy: boolean }>();
const emit = defineEmits<{ submit: [values: Partial<TravelRequirements>] }>();
const values = reactive<Partial<TravelRequirements>>({});
const destinationMode = ref<"destination" | "region">("destination");
const destinationValue = ref("");
const durationMode = ref<"end_date" | "trip_days">("end_date");
const tripDays = ref<number | null>(null);

const complete = computed(() => props.interrupt.missing_fields.every((field) => {
  if (field === "origin") return Boolean(values.origin?.trim());
  if (field === "destination_or_region") return Boolean(destinationValue.value.trim());
  if (field === "start_date") return Boolean(values.start_date);
  if (field === "end_date") {
    return durationMode.value === "end_date" ? Boolean(values.end_date) : Boolean(tripDays.value);
  }
  return true;
}));

function submit(): void {
  if (!complete.value) return;
  const patch: Partial<TravelRequirements> = { ...values };
  if (destinationValue.value.trim()) {
    patch[destinationMode.value] = destinationValue.value.trim();
  }
  if (durationMode.value === "trip_days" && tripDays.value) {
    delete patch.end_date;
    patch.trip_days = tripDays.value;
  }
  emit("submit", patch);
}
</script>

<template>
  <section class="interrupt-card clarification-card" aria-labelledby="clarification-title">
    <header>
      <span class="interrupt-icon"><HelpCircle :size="20" /></span>
      <div>
        <p class="section-kicker">还需要一点信息</p>
        <h3 id="clarification-title">{{ interrupt.question }}</h3>
      </div>
    </header>
    <p v-if="interrupt.error" class="interrupt-error" role="alert">{{ interrupt.error.message }}</p>
    <form @submit.prevent="submit">
      <label v-if="interrupt.missing_fields.includes('origin')" for="origin-value">
        出发地
        <input id="origin-value" v-model="values.origin" :disabled="busy" autocomplete="address-level2" placeholder="例如：上海" />
      </label>

      <fieldset v-if="interrupt.missing_fields.includes('destination_or_region')" class="structured-field">
        <legend>目的地</legend>
        <div class="segmented-choice">
          <label><input v-model="destinationMode" type="radio" value="destination" :disabled="busy" />具体城市</label>
          <label><input v-model="destinationMode" type="radio" value="region" :disabled="busy" />探索地区</label>
        </div>
        <label for="destination-value" class="sr-only">{{ destinationMode === 'destination' ? '具体城市' : '探索地区' }}</label>
        <input
          id="destination-value"
          v-model="destinationValue"
          :disabled="busy"
          autocomplete="address-level2"
          :placeholder="destinationMode === 'destination' ? '例如：杭州' : '例如：江南、华东'"
        />
      </fieldset>

      <label v-if="interrupt.missing_fields.includes('start_date')" for="start-date-value">
        开始日期
        <input id="start-date-value" v-model="values.start_date" type="date" :disabled="busy" />
      </label>

      <fieldset v-if="interrupt.missing_fields.includes('end_date')" class="structured-field">
        <legend>结束日期或天数</legend>
        <div class="segmented-choice">
          <label><input v-model="durationMode" type="radio" value="end_date" :disabled="busy" />结束日期</label>
          <label><input v-model="durationMode" type="radio" value="trip_days" :disabled="busy" />行程天数</label>
        </div>
        <template v-if="durationMode === 'end_date'">
          <label for="end-date-value" class="sr-only">结束日期</label>
          <input id="end-date-value" v-model="values.end_date" type="date" :disabled="busy" />
        </template>
        <template v-else>
          <label for="trip-days-value" class="sr-only">行程天数</label>
          <input id="trip-days-value" v-model.number="tripDays" type="number" min="1" max="7" :disabled="busy" placeholder="1—7 天" />
        </template>
      </fieldset>

      <button class="primary-button" type="submit" :disabled="busy || !complete">
        <LoaderCircle v-if="busy" class="spin" :size="17" />
        <ArrowRight v-else :size="17" />
        继续
      </button>
    </form>
  </section>
</template>
