<!-- 第三步：展示交通、住宿和逐日行程，并支持单日局部编辑。 -->
<script setup lang="ts">
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  BedDouble,
  Check,
  Clock3,
  Download,
  ExternalLink,
  GripVertical,
  Hotel,
  LoaderCircle,
  MapPin,
  Pencil,
  RefreshCw,
  TrainFront,
  X,
} from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import Draggable from "vuedraggable";

import TripMap from "../TripMap.vue";
import {
  editTripDay,
  errorMessage,
  getTrip,
  getTripAlternatives,
  markdownUrl,
} from "../api";
import type {
  BudgetBreakdown,
  DayActivityEdit,
  DayPlan,
  Itinerary,
  Poi,
  RailOption,
} from "../types";

type PlaceKind = "景点" | "餐厅" | "酒店";
type DetailPlace = {
  kind: PlaceKind;
  poiId: string;
  name: string;
  address: string;
  latitude: number;
  longitude: number;
  imageUrl: string | null;
  dayIndex: number;
  date: string;
  theme: string;
  startTime?: string;
  durationMinutes?: number;
  note?: string;
  mealType?: string;
  level?: string;
};

const route = useRoute();
const router = useRouter();
const itinerary = ref<Itinerary | null>(null);
const error = ref("");
const loading = ref(true);
const failedImages = ref(new Set<string>());
const selectedPlace = ref<DetailPlace | null>(null);
const copiedAddress = ref(false);
const activeDayIndex = ref(1);
const editing = ref(false);
const savingEdit = ref(false);
const alternatives = ref<Poi[]>([]);
const editActivities = ref<DayActivityEdit[]>([]);
const budgetLabels: Array<["transport" | "hotel" | "meals" | "tickets" | "other", string]> = [
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
const activeDay = computed(() => itinerary.value?.days.find(
  (day) => day.day_index === activeDayIndex.value,
) || itinerary.value?.days[0]);

onMounted(load);
onBeforeUnmount(() => window.removeEventListener("keydown", handleKeydown));

async function load(): Promise<void> {
  try {
    itinerary.value = await getTrip(String(route.params.tripId));
    activeDayIndex.value = itinerary.value.days[0]?.day_index || 1;
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    loading.value = false;
  }
}

function money(value: number | null | undefined): string {
  return value === null || value === undefined
    ? "待确认"
    : `¥${Math.round(value).toLocaleString("zh-CN")}`;
}

function imageVisible(key: string, url: string | null): boolean {
  return Boolean(url) && !failedImages.value.has(key);
}

function hideImage(key: string): void {
  failedImages.value = new Set(failedImages.value).add(key);
}

function sourceLabel(source: DayPlan["weather"]["source"]): string {
  if (!source) return "来源未知";
  const labels: Record<string, string> = {
    open_meteo: "Open-Meteo 预报",
    amap: "高德实时",
    local_estimate: "本地估算",
    osm: "OSM 静态",
    unknown: "来源未知",
  };
  return labels[source.provider] || "来源未知";
}

function openPlace(day: DayPlan, poiId: string): void {
  const spot = day.activities.find((item) => item.poi_id === poiId);
  const meal = day.meals.find((item) => item.poi_id === poiId);
  const hotel = day.hotel?.poi_id === poiId ? day.hotel : null;
  if (!spot && !meal && !hotel) return;
  selectedPlace.value = spot ? {
    kind: "景点", poiId: spot.poi_id, name: spot.name, address: spot.address,
    latitude: spot.latitude, longitude: spot.longitude, imageUrl: spot.image_url,
    dayIndex: day.day_index, date: day.date, theme: day.theme,
    startTime: spot.start_time, durationMinutes: spot.duration_minutes, note: spot.note,
  } : meal ? {
    kind: "餐厅", poiId: meal.poi_id, name: meal.name, address: meal.address,
    latitude: meal.latitude, longitude: meal.longitude, imageUrl: meal.image_url,
    dayIndex: day.day_index, date: day.date, theme: day.theme, mealType: meal.meal_type,
  } : {
    kind: "酒店", poiId: hotel!.poi_id, name: hotel!.name, address: hotel!.address,
    latitude: hotel!.latitude, longitude: hotel!.longitude, imageUrl: hotel!.image_url,
    dayIndex: day.day_index, date: day.date, theme: day.theme, level: hotel!.level,
  };
  window.addEventListener("keydown", handleKeydown);
}

function closePlace(): void {
  selectedPlace.value = null;
  copiedAddress.value = false;
  window.removeEventListener("keydown", handleKeydown);
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") { closePlace(); editing.value = false; }
}

async function copyAddress(): Promise<void> {
  const address = selectedPlace.value?.address;
  if (!address || !navigator.clipboard) return;
  await navigator.clipboard.writeText(address);
  copiedAddress.value = true;
  window.setTimeout(() => { copiedAddress.value = false; }, 1600);
}

function openAmap(): void {
  const place = selectedPlace.value;
  if (!place) return;
  const keyword = encodeURIComponent(`${place.name} ${place.address}`);
  window.open(`https://uri.amap.com/search?keyword=${keyword}`, "_blank");
}

async function openEditor(): Promise<void> {
  if (!itinerary.value || !activeDay.value) return;
  error.value = "";
  try {
    const response = await getTripAlternatives(itinerary.value.trip_id);
    alternatives.value = response.attractions;
    editActivities.value = activeDay.value.activities.map((item) => ({
      poi_id: item.poi_id,
      start_time: item.start_time,
      duration_minutes: item.duration_minutes,
    }));
    editing.value = true;
    window.addEventListener("keydown", handleKeydown);
  } catch (requestError) {
    error.value = errorMessage(requestError);
  }
}

function moveActivity(index: number, offset: number): void {
  const target = index + offset;
  if (target < 0 || target >= editActivities.value.length) return;
  const items = [...editActivities.value];
  [items[index], items[target]] = [items[target], items[index]];
  editActivities.value = items;
}

function addActivity(): void {
  const used = new Set(editActivities.value.map((item) => item.poi_id));
  const candidate = alternatives.value.find((item) => !used.has(item.id));
  if (!candidate || editActivities.value.length >= 4) return;
  editActivities.value.push({ poi_id: candidate.id, start_time: "15:00", duration_minutes: 120 });
}

function removeActivity(index: number): void {
  editActivities.value.splice(index, 1);
}

async function saveEditor(): Promise<void> {
  if (!itinerary.value || !activeDay.value) return;
  savingEdit.value = true;
  try {
    itinerary.value = await editTripDay(
      itinerary.value.trip_id,
      activeDay.value.day_index,
      {
        expected_revision: itinerary.value.revision || 1,
        activities: editActivities.value,
      },
    );
    editing.value = false;
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    savingEdit.value = false;
  }
}

function poiName(poiId: string): string {
  return alternatives.value.find((item) => item.id === poiId)?.name || poiId;
}

function railSummary(option: RailOption | null | undefined): string {
  if (!option) return "自行安排";
  return `${option.train_code} · ${option.departure_time}–${option.arrival_time}`;
}
</script>

<template>
  <section v-if="loading" class="loading-panel"><LoaderCircle class="spin" :size="22" />正在加载行程…</section>
  <section v-else-if="error && !itinerary" class="loading-panel"><p class="error-message">{{ error }}</p><button class="secondary-button" @click="router.push('/')">返回规划</button></section>
  <section v-else-if="itinerary" class="workspace-page result-workspace">
    <header class="workspace-heading result-heading">
      <div><p class="section-kicker">可执行行程 · 版本 {{ itinerary.revision || 1 }}</p><h1>{{ itinerary.destination }}旅行计划</h1><p>{{ itinerary.summary }}</p></div>
      <div class="header-actions"><button class="secondary-button" @click="router.push('/')"><RefreshCw :size="16" />重新规划</button><a class="secondary-button" :href="markdownUrl(itinerary.trip_id)" target="_blank"><Download :size="16" />导出</a></div>
    </header>
    <p v-if="error" class="error-message global-error" role="alert">{{ error }}</p>

    <section class="journey-strip" aria-label="往返交通与住宿">
      <article><span class="journey-icon rail"><TrainFront :size="19" /></span><div><small>去程 · {{ itinerary.start_date }}</small><strong>{{ railSummary(itinerary.intercity?.outbound) }}</strong><span>{{ itinerary.intercity?.outbound ? `${itinerary.intercity.outbound.from_station} → ${itinerary.intercity.outbound.to_station}` : "请自行确认交通" }}</span></div></article>
      <article><span class="journey-icon hotel"><BedDouble :size="19" /></span><div><small>住宿 · {{ itinerary.accommodation?.nights || 0 }} 晚</small><strong>{{ itinerary.accommodation?.nights === 0 ? "无需住宿" : itinerary.accommodation?.hotel?.name || "自行安排" }}</strong><span>{{ itinerary.accommodation?.nights === 0 ? "一日行程不计算住宿费用" : itinerary.accommodation?.hotel?.address || "住宿费用可能未计入预算" }}</span></div></article>
      <article><span class="journey-icon rail"><TrainFront :size="19" /></span><div><small>返程 · {{ itinerary.end_date }}</small><strong>{{ railSummary(itinerary.intercity?.return_trip) }}</strong><span>{{ itinerary.intercity?.return_trip ? `${itinerary.intercity.return_trip.from_station} → ${itinerary.intercity.return_trip.to_station}` : "请自行确认交通" }}</span></div></article>
    </section>

    <div v-if="itinerary.warnings.length" class="notice-list"><p v-for="warning in itinerary.warnings" :key="warning">{{ warning }}</p></div>

    <nav class="day-tabs" aria-label="每日行程"><button v-for="day in itinerary.days" :key="day.day_index" :class="{ active: activeDayIndex === day.day_index }" @click="activeDayIndex = day.day_index"><span>第 {{ day.day_index }} 天</span><small>{{ day.date.slice(5) }} · {{ day.theme }}</small></button></nav>

    <div v-if="activeDay" class="result-layout">
      <article class="day-panel">
        <header class="day-heading"><div><span class="day-number">DAY {{ String(activeDay.day_index).padStart(2, "0") }}</span><h2>{{ activeDay.theme }}</h2><p class="muted-text">{{ activeDay.date }}</p></div><div class="day-actions"><span class="weather">{{ activeDay.weather.warning || `${activeDay.weather.day_weather || "未知"} ${activeDay.weather.day_temperature || ""}℃` }}<small>{{ sourceLabel(activeDay.weather.source) }}</small></span><button v-if="itinerary.planning_session_id" class="secondary-button" @click="openEditor"><Pencil :size="16" />编辑当天</button></div></header>

        <div v-if="activeDay.activities.length" class="timeline"><div v-for="spot in activeDay.activities" :key="spot.poi_id" class="timeline-item"><span class="timeline-time">{{ spot.start_time }}</span><button class="place-row place-button" :aria-label="`查看${spot.name}详情`" @click="openPlace(activeDay, spot.poi_id)"><img v-if="imageVisible(`spot-${spot.poi_id}`, spot.image_url)" class="place-image spot-image" :src="spot.image_url || ''" :alt="`${spot.name}图片`" loading="lazy" @error="hideImage(`spot-${spot.poi_id}`)" /><div v-else class="place-image spot-image image-placeholder"><MapPin :size="22" /><span>暂无图片</span></div><div><h3>{{ spot.name }}</h3><p>{{ spot.note || "自由游览" }}</p><small>{{ spot.address }} · {{ spot.duration_minutes }} 分钟</small></div></button></div></div>
        <p v-else class="empty-copy">当天受交通时间约束，没有强行安排景点。</p>

        <div v-if="activeDay.meals.length" class="inline-info"><strong>用餐</strong><button v-for="meal in activeDay.meals" :key="meal.poi_id" class="compact-place place-button" @click="openPlace(activeDay, meal.poi_id)">{{ meal.meal_type }} · {{ meal.name }}</button></div>
        <div v-if="activeDay.hotel" class="inline-info"><strong>住宿</strong><button class="compact-place place-button" @click="openPlace(activeDay, activeDay.hotel.poi_id)">{{ activeDay.hotel.name }} · {{ activeDay.hotel.address }}</button></div>
        <div v-if="activeDay.routes.length" class="route-list"><article v-for="routeItem in activeDay.routes" :key="`${routeItem.from_poi_id}-${routeItem.to_poi_id}`"><span class="route-icon"><ArrowRight :size="15" /></span><div><strong>{{ routeItem.mode }} · {{ routeItem.distance_km }} 公里 / {{ routeItem.duration_minutes }} 分钟</strong><small>{{ sourceLabel(routeItem.source) }}</small><p v-for="line in routeItem.transit_lines || []" :key="line.name">{{ line.name }}：{{ line.departure_stop }} → {{ line.arrival_stop }}</p></div></article></div>
        <div v-if="activeDay.budget" class="day-budget"><strong>当日估算 {{ money(activeDay.budget.total) }}</strong><span v-for="[key, label] in budgetLabels" :key="key">{{ label }} {{ money(activeDay.budget[key]) }}</span></div><p v-else class="legacy-budget">旧行程暂无每日预算明细。</p>
        <TripMap :day="activeDay" @select-place="openPlace(activeDay, $event)" />
      </article>

      <aside class="result-sidebar"><section class="sidebar-section"><p class="section-kicker">预算估算</p><strong class="budget-total">{{ money(itinerary.budget.total) }}</strong><div v-for="item in budgetItems" :key="item[0]" class="budget-line"><span>{{ item[0] }}</span><span>{{ money(Number(item[1])) }}</span></div><p class="budget-note">未知票价和房价不会伪造，也不会计入总额。</p></section><section class="sidebar-section"><p class="section-kicker">出发前提醒</p><ul><li v-for="tip in itinerary.tips" :key="tip">{{ tip }}</li><li>地图、车次和价格请在出发前再次确认。</li></ul></section></aside>
    </div>

    <div v-if="selectedPlace" class="drawer-backdrop" @click.self="closePlace"><aside class="detail-drawer" aria-label="地点详情"><button class="icon-button drawer-close" aria-label="关闭详情" @click="closePlace"><X :size="20" /></button><span class="source-tag">{{ selectedPlace.kind }}</span><h2>{{ selectedPlace.name }}</h2><p class="muted-text">{{ selectedPlace.date }} · 第 {{ selectedPlace.dayIndex }} 天</p><img v-if="imageVisible(`detail-${selectedPlace.poiId}`, selectedPlace.imageUrl)" class="detail-image" :src="selectedPlace.imageUrl || ''" :alt="`${selectedPlace.name}图片`" @error="hideImage(`detail-${selectedPlace.poiId}`)" /><div v-else class="detail-image image-placeholder"><MapPin :size="28" />暂无图片</div><dl class="detail-list"><div><dt>地址</dt><dd>{{ selectedPlace.address || "暂无地址" }}</dd></div><div v-if="selectedPlace.kind === '景点'"><dt>安排时间</dt><dd>{{ selectedPlace.startTime }} · 游览 {{ selectedPlace.durationMinutes }} 分钟</dd></div><div v-if="selectedPlace.kind === '景点'"><dt>行程备注</dt><dd>{{ selectedPlace.note || "暂无备注" }}</dd></div><div v-if="selectedPlace.kind === '餐厅'"><dt>用餐类型</dt><dd>{{ selectedPlace.mealType }}</dd></div><div v-if="selectedPlace.kind === '酒店'"><dt>住宿档次</dt><dd>{{ selectedPlace.level }}</dd></div><div><dt>数据来源</dt><dd>已生成行程中的真实 POI 数据</dd></div></dl><p class="detail-disclaimer">当前详情不补写评分、电话、营业时间或实时价格。</p><div class="detail-actions"><button class="secondary-button" :disabled="!selectedPlace.address" @click="copyAddress">{{ copiedAddress ? "地址已复制" : "复制地址" }}</button><button class="primary-button" @click="openAmap"><ExternalLink :size="16" />在高德中打开</button></div></aside></div>

    <div v-if="editing && activeDay" class="drawer-backdrop" @click.self="editing = false"><aside class="detail-drawer edit-drawer" aria-label="编辑当天行程"><button class="icon-button drawer-close" aria-label="关闭编辑" @click="editing = false"><X :size="20" /></button><span class="source-tag">编辑第 {{ activeDay.day_index }} 天</span><h2>调整景点与时间</h2><p class="muted-text">拖拽或使用箭头排序，保存后只重算当天路线和预算。</p><Draggable v-model="editActivities" item-key="poi_id" handle=".drag-handle" class="edit-list"><template #item="{ element, index }"><article class="edit-row"><button class="icon-button drag-handle" title="拖拽排序" aria-label="拖拽排序"><GripVertical :size="18" /></button><div class="edit-fields"><label><span>景点</span><select v-model="element.poi_id"><option v-for="poi in alternatives" :key="poi.id" :value="poi.id">{{ poi.name }}</option></select></label><label><span>开始</span><input v-model="element.start_time" type="time" /></label><label><span>分钟</span><input v-model.number="element.duration_minutes" type="number" min="30" max="480" step="30" /></label></div><div class="edit-actions"><button class="icon-button" title="上移" aria-label="上移" :disabled="index === 0" @click="moveActivity(index, -1)"><ArrowUp :size="17" /></button><button class="icon-button" title="下移" aria-label="下移" :disabled="index === editActivities.length - 1" @click="moveActivity(index, 1)"><ArrowDown :size="17" /></button><button class="icon-button danger" title="移除" aria-label="移除" @click="removeActivity(index)"><X :size="17" /></button></div></article></template></Draggable><button class="secondary-button full-width" :disabled="editActivities.length >= 4 || !alternatives.some((item) => !editActivities.some((activity) => activity.poi_id === item.id))" @click="addActivity">添加候选景点</button><footer class="drawer-footer"><button class="secondary-button" @click="editing = false">取消</button><button class="primary-button" :disabled="savingEdit" @click="saveEditor"><LoaderCircle v-if="savingEdit" class="spin" :size="17" /><Check v-else :size="17" />{{ savingEdit ? "正在重算路线…" : "保存并重算" }}</button></footer></aside></div>
  </section>
</template>
