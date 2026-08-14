<!-- 第一步：创建持久规划会话，立即进入数据发现页。 -->
<script setup lang="ts">
import { CalendarDays, MapPin, Route, Search, Users } from "@lucide/vue";
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { createPlanningSession, errorMessage } from "../api";
import type { PlanningRequest } from "../types";

const router = useRouter();
const loading = ref(false);
const error = ref("");
const preferenceOptions = ["自然风景", "历史人文", "拍照打卡", "美食", "亲子", "购物"];
const dietOptions = ["清淡", "素食", "清真", "地方特色"];
const form = reactive<PlanningRequest>({
  origin: "北京",
  destination: "杭州",
  start_date: addDays(today(), 7),
  end_date: addDays(today(), 10),
  travelers: 2,
  budget: 5000,
  pace: "适中",
  hotel_level: "舒适",
  transport_mode: "auto",
  preferences: ["自然风景", "美食"],
  dietary_preferences: [],
  notes: "",
});

const days = computed(() => {
  const start = new Date(`${form.start_date}T00:00:00`).getTime();
  const end = new Date(`${form.end_date}T00:00:00`).getTime();
  return Math.round((end - start) / 86400000) + 1;
});

function toggle(list: string[], value: string): void {
  const index = list.indexOf(value);
  index === -1 ? list.push(value) : list.splice(index, 1);
}

async function submit(): Promise<void> {
  error.value = "";
  if (!form.origin.trim() || !form.destination.trim()) {
    error.value = "请填写出发地和目的地。";
    return;
  }
  if (days.value < 1 || days.value > 7) {
    error.value = "行程日期需要在 1～7 天内。";
    return;
  }
  loading.value = true;
  try {
    const key = globalThis.crypto?.randomUUID?.()
      || `${Date.now()}-${form.origin}-${form.destination}`;
    const session = await createPlanningSession(form, key);
    await router.push(`/planning/${session.session_id}`);
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
  const item = new Date(`${value}T00:00:00`);
  item.setDate(item.getDate() + offset);
  return item.toISOString().slice(0, 10);
}
</script>

<template>
  <section class="workspace-page plan-workspace">
    <header class="workspace-heading">
      <div>
        <p class="section-kicker">新建旅行计划</p>
        <h1>整理出发需求</h1>
        <p>车票、酒店、天气与地点会在下一步并行查询，你可以边看结果边做选择。</p>
      </div>
      <ol class="stepper" aria-label="规划步骤">
        <li class="active"><span>1</span>需求</li>
        <li><span>2</span>选择</li>
        <li><span>3</span>行程</li>
      </ol>
    </header>

    <form class="workbench-form" @submit.prevent="submit">
      <section class="form-section">
        <div class="form-section-title"><Route :size="20" /><div><h2>路线与日期</h2><p>用于查询往返车次和住宿日期</p></div></div>
        <div class="form-grid two-columns">
          <label><span><MapPin :size="15" />出发地 *</span><input v-model="form.origin" autocomplete="address-level2" placeholder="例如：北京" /></label>
          <label><span><Search :size="15" />目的地 *</span><input v-model="form.destination" autocomplete="address-level2" placeholder="例如：杭州" /></label>
        </div>
        <div class="form-grid two-columns">
          <label><span><CalendarDays :size="15" />去程 / 入住</span><input v-model="form.start_date" type="date" /></label>
          <label><span><CalendarDays :size="15" />返程 / 退房</span><input v-model="form.end_date" type="date" /></label>
        </div>
        <p class="field-note">{{ days > 0 ? `共 ${days} 天` : "日期无效" }}，最多支持 7 天。</p>
      </section>

      <section class="form-section">
        <div class="form-section-title"><Users :size="20" /><div><h2>预算与节奏</h2><p>预算仅用于提醒，不会改变真实候选</p></div></div>
        <div class="form-grid three-columns">
          <label><span>出行人数</span><input v-model.number="form.travelers" type="number" min="1" max="20" /></label>
          <label><span>总预算（元）</span><input v-model.number="form.budget" type="number" min="0" step="100" /></label>
          <label><span>旅行节奏</span><select v-model="form.pace"><option>轻松</option><option>适中</option><option>紧凑</option></select></label>
        </div>
        <div class="form-grid two-columns">
          <label><span>住宿档次</span><select v-model="form.hotel_level"><option>经济</option><option>舒适</option><option>品质</option></select></label>
          <label><span>市内交通</span><select v-model="form.transport_mode"><option value="auto">自动选择</option><option value="walk">步行估算</option><option value="driving">驾车估算</option><option value="transit">公交 / 地铁</option><option value="realtime_driving">实时驾车</option></select></label>
        </div>
      </section>

      <section class="form-section">
        <div class="form-section-title"><Search :size="20" /><div><h2>偏好与说明</h2><p>帮助确定性规划器安排景点密度</p></div></div>
        <fieldset><legend>旅行偏好</legend><div class="chip-group"><button v-for="item in preferenceOptions" :key="item" type="button" :class="['chip', { active: form.preferences.includes(item) }]" :aria-pressed="form.preferences.includes(item)" @click="toggle(form.preferences, item)">{{ item }}</button></div></fieldset>
        <fieldset><legend>饮食偏好</legend><div class="chip-group"><button v-for="item in dietOptions" :key="item" type="button" :class="['chip', { active: form.dietary_preferences.includes(item) }]" :aria-pressed="form.dietary_preferences.includes(item)" @click="toggle(form.dietary_preferences, item)">{{ item }}</button></div></fieldset>
        <label><span>补充说明</span><textarea v-model="form.notes" rows="3" maxlength="500" placeholder="例如：首日希望轻松一些，不安排夜间活动。" /></label>
      </section>

      <footer class="form-footer">
        <p v-if="error" class="error-message" role="alert">{{ error }}</p>
        <p v-else>提交后 1 秒内进入查询进度页，不需要等待整份行程生成。</p>
        <button class="primary-button" type="submit" :disabled="loading">
          <Search :size="18" />{{ loading ? "正在创建任务…" : "查询车票与住宿" }}
        </button>
      </footer>
    </form>
  </section>
</template>
