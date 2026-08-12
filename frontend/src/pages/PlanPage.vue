<!-- 创建页：收集旅行需求并跳转到生成结果，不保存跨页面全局状态。 -->
<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { createTrip, errorMessage } from "../api";
import type { TravelRequest } from "../types";

const router = useRouter();
const loading = ref(false);
const error = ref("");
const preferenceOptions = ["自然风景", "历史人文", "拍照打卡", "美食", "亲子", "购物"];
const dietOptions = ["清淡", "素食", "清真", "地方特色"];
const form = reactive<TravelRequest>({
  destination: "杭州",
  start_date: today(),
  end_date: addDays(today(), 2),
  travelers: 2,
  budget: 3000,
  pace: "适中",
  hotel_level: "舒适",
  preferences: ["自然风景", "美食"],
  dietary_preferences: [],
  notes: "",
});

const days = computed(() => {
  const start = new Date(form.start_date).getTime();
  const end = new Date(form.end_date).getTime();
  return Math.round((end - start) / 86400000) + 1;
});

function toggle(list: string[], value: string): void {
  const index = list.indexOf(value);
  index === -1 ? list.push(value) : list.splice(index, 1);
}

async function submit(): Promise<void> {
  error.value = "";
  if (!form.destination.trim()) {
    error.value = "请填写目的地。";
    return;
  }
  if (days.value < 1 || days.value > 7) {
    error.value = "行程日期需要在 1～7 天内。";
    return;
  }
  loading.value = true;
  try {
    const itinerary = await createTrip(form);
    await router.push(`/result/${itinerary.trip_id}`);
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    loading.value = false;
  }
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function addDays(value: string, offset: number): string {
  const date = new Date(`${value}T00:00:00`);
  date.setDate(date.getDate() + offset);
  return date.toISOString().slice(0, 10);
}
</script>

<template>
  <section class="hero-grid">
    <div class="hero-copy">
      <p class="eyebrow">PERSONAL TRAVEL PLANNER</p>
      <h1>把想去的地方，<em>变成一份好走的行程。</em></h1>
      <p class="hero-description">输入你的出发计划，OpenZLTravel 会结合真实地点、天气和路线，整理一份清晰可执行的旅行方案。</p>
      <div class="feature-row">
        <span>真实地点数据</span><span>结构化行程</span><span>预算估算</span>
      </div>
    </div>

    <form class="card planner-form" @submit.prevent="submit">
      <div class="section-heading">
        <div><p class="eyebrow">START HERE</p><h2>规划一次旅行</h2></div>
        <span class="step-badge">01 / 01</span>
      </div>
      <label>目的地<input v-model="form.destination" placeholder="例如：杭州、成都、大理" /></label>
      <div class="form-grid two-columns">
        <label>开始日期<input v-model="form.start_date" type="date" /></label>
        <label>结束日期<input v-model="form.end_date" type="date" /></label>
      </div>
      <div class="trip-meta"><span>共 {{ days > 0 ? days : "-" }} 天</span><span>最多支持 7 天</span></div>
      <div class="form-grid two-columns">
        <label>出行人数<input v-model.number="form.travelers" type="number" min="1" max="20" /></label>
        <label>预算（元）<input v-model.number="form.budget" type="number" min="0" step="100" /></label>
      </div>
      <div class="form-grid two-columns">
        <label>旅行节奏<select v-model="form.pace"><option>轻松</option><option>适中</option><option>紧凑</option></select></label>
        <label>住宿偏好<select v-model="form.hotel_level"><option>经济</option><option>舒适</option><option>品质</option></select></label>
      </div>
      <fieldset><legend>旅行偏好</legend><button v-for="item in preferenceOptions" :key="item" type="button" :class="['chip', { active: form.preferences.includes(item) }]" @click="toggle(form.preferences, item)">{{ item }}</button></fieldset>
      <fieldset><legend>饮食偏好</legend><button v-for="item in dietOptions" :key="item" type="button" :class="['chip', { active: form.dietary_preferences.includes(item) }]" @click="toggle(form.dietary_preferences, item)">{{ item }}</button></fieldset>
      <label>补充说明<textarea v-model="form.notes" rows="3" maxlength="500" placeholder="例如：不想走太多路，希望安排一晚夜景。" /></label>
      <p v-if="error" class="error-message">{{ error }}</p>
      <button class="primary-button" type="submit" :disabled="loading">{{ loading ? "正在整理真实数据…" : "生成我的行程" }} <span>→</span></button>
    </form>
  </section>
</template>
