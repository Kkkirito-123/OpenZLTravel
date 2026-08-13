<!-- 结果页：读取并展示已经保存的结构化行程。 -->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import TripMap from "../TripMap.vue";
import { errorMessage, getTrip, markdownUrl } from "../api";
import type { BudgetBreakdown, Itinerary } from "../types";

const route = useRoute();
const router = useRouter();
const itinerary = ref<Itinerary | null>(null);
const error = ref("");
const loading = ref(true);
const failedImages = ref(new Set<string>());
const budgetLabels: [keyof BudgetBreakdown, string][] = [
  ["transport", "交通"],
  ["hotel", "住宿"],
  ["meals", "餐饮"],
  ["tickets", "门票"],
  ["other", "其他"],
];
const budgetItems = computed(() => {
  if (!itinerary.value) return [];
  return budgetLabels.map(([key, label]) => [label, itinerary.value!.budget[key]] as const);
});

onMounted(async () => {
  try {
    itinerary.value = await getTrip(String(route.params.tripId));
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    loading.value = false;
  }
});

function money(value: number): string {
  return `¥${Math.round(value).toLocaleString("zh-CN")}`;
}

function imageVisible(key: string, url: string | null): boolean {
  return Boolean(url) && !failedImages.value.has(key);
}

function hideImage(key: string): void {
  failedImages.value = new Set(failedImages.value).add(key);
}
</script>

<template>
  <section v-if="loading" class="state-card">正在加载行程…</section>
  <section v-else-if="error" class="state-card">
    <p class="error-message">{{ error }}</p>
    <button class="secondary-button" @click="router.push('/')">返回规划</button>
  </section>
  <section v-else-if="itinerary" class="result-page">
    <div class="result-header">
      <div>
        <p class="eyebrow">YOUR ITINERARY</p>
        <h1>{{ itinerary.destination }}，准备出发。</h1>
        <p class="lead">{{ itinerary.summary }}</p>
        <p class="muted">
          {{ itinerary.start_date }} — {{ itinerary.end_date }} · {{ itinerary.travelers }} 人
        </p>
      </div>
      <div class="header-actions">
        <button class="secondary-button" @click="router.push('/')">重新规划</button>
        <a class="secondary-button" :href="markdownUrl(itinerary.trip_id)" target="_blank">
          导出 Markdown
        </a>
      </div>
    </div>

    <div v-if="itinerary.warnings.length" class="notice-list">
      <p v-for="warning in itinerary.warnings" :key="warning">提示：{{ warning }}</p>
    </div>

    <div class="result-layout">
      <div class="day-list">
        <article v-for="day in itinerary.days" :key="day.day_index" class="card day-card">
          <div class="day-heading">
            <div>
              <span class="day-number">
                DAY {{ String(day.day_index).padStart(2, "0") }}
              </span>
              <h2>{{ day.theme }}</h2>
              <p class="muted">{{ day.date }}</p>
            </div>
            <span class="weather">
              {{
                day.weather.warning ||
                `${day.weather.day_weather || "天气未知"} ${day.weather.day_temperature || ""}`
              }}
            </span>
          </div>

          <div class="timeline">
            <div v-for="spot in day.activities" :key="spot.poi_id" class="timeline-item">
              <span class="timeline-time">{{ spot.start_time }}</span>
              <div class="place-row">
                <img
                  v-if="imageVisible(`spot-${spot.poi_id}`, spot.image_url)"
                  class="place-image spot-image"
                  :src="spot.image_url || ''"
                  :alt="`${spot.name}图片`"
                  loading="lazy"
                  @error="hideImage(`spot-${spot.poi_id}`)"
                />
                <div v-else class="place-image spot-image image-placeholder">暂无图片</div>
                <div>
                  <h3>{{ spot.name }}</h3>
                  <p>{{ spot.note || "自由游览" }}</p>
                  <small>{{ spot.address }} · {{ spot.duration_minutes }} 分钟</small>
                </div>
              </div>
            </div>
          </div>

          <div v-if="day.meals.length" class="inline-info place-list">
            <strong>用餐</strong>
            <span v-for="meal in day.meals" :key="meal.poi_id" class="compact-place">
              <img
                v-if="imageVisible(`meal-${meal.poi_id}`, meal.image_url)"
                class="place-image compact-image"
                :src="meal.image_url || ''"
                :alt="`${meal.name}图片`"
                loading="lazy"
                @error="hideImage(`meal-${meal.poi_id}`)"
              />
              <span v-else class="place-image compact-image image-placeholder">无图</span>
              <span>{{ meal.meal_type }} · {{ meal.name }}</span>
            </span>
          </div>

          <div v-if="day.hotel" class="inline-info place-list">
            <strong>住宿</strong>
            <span class="compact-place">
              <img
                v-if="imageVisible(`hotel-${day.hotel.poi_id}`, day.hotel.image_url)"
                class="place-image compact-image"
                :src="day.hotel.image_url || ''"
                :alt="`${day.hotel.name}图片`"
                loading="lazy"
                @error="hideImage(`hotel-${day.hotel.poi_id}`)"
              />
              <span v-else class="place-image compact-image image-placeholder">无图</span>
              <span>{{ day.hotel.name }} · {{ day.hotel.address }}</span>
            </span>
          </div>

          <div v-if="day.routes.length" class="route-list">
            <span
              v-for="routeItem in day.routes"
              :key="`${routeItem.from_poi_id}-${routeItem.to_poi_id}`"
            >
              {{ routeItem.mode }}约 {{ routeItem.distance_km }} 公里 /
              {{ routeItem.duration_minutes }} 分钟
            </span>
          </div>

          <div v-if="day.budget" class="day-budget">
            <strong>当日估算 {{ money(day.budget.total) }}</strong>
            <span v-for="[key, label] in budgetLabels" :key="key">
              {{ label }} {{ money(day.budget[key]) }}
            </span>
          </div>
          <p v-else class="legacy-budget">旧行程暂无每日预算明细。</p>
          <TripMap :day="day" />
        </article>
      </div>

      <aside class="result-sidebar">
        <div class="card budget-card">
          <p class="eyebrow">ESTIMATED BUDGET</p>
          <strong class="budget-total">{{ money(itinerary.budget.total) }}</strong>
          <div v-for="item in budgetItems" :key="item[0]" class="budget-line">
            <span>{{ item[0] }}</span>
            <span>{{ money(Number(item[1])) }}</span>
          </div>
          <p class="budget-note">价格为根据行程规模计算的估算值。</p>
        </div>
        <div class="card tips-card">
          <p class="eyebrow">NOTES</p>
          <h3>出发前提醒</h3>
          <ul>
            <li v-for="tip in itinerary.tips" :key="tip">{{ tip }}</li>
            <li>地图、天气和地点信息来自外部服务，请出发前再次确认。</li>
          </ul>
        </div>
      </aside>
    </div>
  </section>
</template>
