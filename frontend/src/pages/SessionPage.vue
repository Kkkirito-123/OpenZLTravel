<!-- 第二步：轮询并行发现状态，选择往返车次与住宿。 -->
<script setup lang="ts">
import {
  ArrowRight,
  BedDouble,
  Check,
  CircleAlert,
  Clock3,
  CloudSun,
  ExternalLink,
  Hotel,
  LoaderCircle,
  MapPin,
  RefreshCw,
  TrainFront,
  X,
} from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import {
  errorMessage,
  generatePlanningSession,
  getHotelDetail,
  getPlanningSession,
  getReadiness,
  loadRailTransfers,
  retryPlanningSession,
  updatePlanningSelection,
} from "../api";
import type {
  HotelDetail,
  HotelOption,
  PlanningSelection,
  PlanningSession,
  RailDirection,
  RailOption,
} from "../types";

const route = useRoute();
const router = useRouter();
const sessionId = String(route.params.sessionId);
const session = ref<PlanningSession | null>(null);
const error = ref("");
const activeRailTab = ref<RailDirection>("outbound");
const trainType = ref("all");
const departurePeriod = ref("all");
const availableOnly = ref(false);
const sortBy = ref<"departure" | "duration" | "price">("departure");
const loadingTransfers = ref<RailDirection | null>(null);
const loadingDetail = ref(false);
const detail = ref<HotelDetail | null>(null);
const generating = ref(false);
const selectedSeats = reactive<Record<string, string>>({});
const failedHotelImages = ref(new Set<string>());
const savingSelection = ref<string | null>(null);
const selectionNotice = ref("");
const hotelProviderStatus = ref("loading");
let timer: number | null = null;

const selection = computed<PlanningSelection>(() => session.value?.selection || {
  outbound: null,
  return_trip: null,
  hotel_id: null,
  self_arranged_outbound: false,
  self_arranged_return: false,
  self_arranged_hotel: false,
});
const railOptions = computed(() => {
  if (!session.value) return [];
  const direct = activeRailTab.value === "outbound"
    ? session.value.outbound_options : session.value.return_options;
  const transfers = activeRailTab.value === "outbound"
    ? session.value.outbound_transfers : session.value.return_transfers;
  return filterAndSortRail([...direct, ...transfers]);
});
const selectedHotel = computed(() =>
  session.value?.hotel_options.find((item) => item.hotel_id === selection.value.hotel_id),
);
const oneDayTrip = computed(() => session.value?.request.start_date === session.value?.request.end_date);
const canGenerate = computed(() => {
  const item = selection.value;
  return Boolean(item.outbound || item.self_arranged_outbound)
    && Boolean(item.return_trip || item.self_arranged_return)
    && Boolean(oneDayTrip.value || item.hotel_id || item.self_arranged_hotel);
});
const discoveryReady = computed(() => session.value?.status === "awaiting_selection");

onMounted(() => {
  void load();
  void loadReadiness();
});
onBeforeUnmount(stopPolling);
watch(() => session.value?.status, (value) => {
  if (value === "searching" || value === "generating") startPolling();
  else stopPolling();
  if (value !== "generating") generating.value = false;
  if (value === "completed" && session.value?.trip_id) {
    void router.replace(`/result/${session.value.trip_id}`);
  }
});

async function load(): Promise<void> {
  try {
    session.value = await getPlanningSession(sessionId);
    error.value = "";
  } catch (requestError) {
    error.value = errorMessage(requestError);
  }
}

async function loadReadiness(): Promise<void> {
  try {
    hotelProviderStatus.value = (await getReadiness()).hotel_provider;
  } catch {
    hotelProviderStatus.value = "unknown";
  }
}

function startPolling(): void {
  if (timer !== null) return;
  timer = window.setInterval(load, 1000);
}

function stopPolling(): void {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
}

function allRail(direction: RailDirection): RailOption[] {
  if (!session.value) return [];
  return direction === "outbound"
    ? [...session.value.outbound_options, ...session.value.outbound_transfers]
    : [...session.value.return_options, ...session.value.return_transfers];
}

function filterAndSortRail(values: RailOption[]): RailOption[] {
  const filtered = values.filter((item) => {
    const typeMatches = trainType.value === "all" || item.train_code.startsWith(trainType.value);
    const hour = Number(item.departure_time.split(":")[0] || 0);
    const periodMatches = departurePeriod.value === "all"
      || (departurePeriod.value === "morning" && hour < 12)
      || (departurePeriod.value === "afternoon" && hour >= 12 && hour < 18)
      || (departurePeriod.value === "evening" && hour >= 18);
    return typeMatches && periodMatches && (!availableOnly.value || item.has_ticket);
  });
  return [...filtered].sort((left, right) => {
    if (sortBy.value === "duration") return left.duration_minutes - right.duration_minutes;
    if (sortBy.value === "price") return (left.price_from ?? Infinity) - (right.price_from ?? Infinity);
    return left.departure_time.localeCompare(right.departure_time);
  });
}

async function chooseRail(direction: RailDirection, option: RailOption): Promise<void> {
  if (!session.value) return;
  const field = direction === "outbound" ? "outbound" : "return_trip";
  const selfField = direction === "outbound" ? "self_arranged_outbound" : "self_arranged_return";
  const seat = selectedSeats[option.option_id] || option.seats[0]?.name || null;
  await saveSelection(
    { ...selection.value, [field]: { option_id: option.option_id, seat_type: seat }, [selfField]: false },
    option.option_id,
    direction === "outbound" ? "去程车次已选择" : "返程车次已选择",
  );
}

async function chooseSelfArranged(kind: RailDirection | "hotel"): Promise<void> {
  const next = { ...selection.value };
  if (kind === "outbound") { next.outbound = null; next.self_arranged_outbound = true; }
  if (kind === "return") { next.return_trip = null; next.self_arranged_return = true; }
  if (kind === "hotel") { next.hotel_id = null; next.self_arranged_hotel = true; }
  await saveSelection(
    next,
    `self-${kind}`,
    `${kind === "hotel" ? "住宿" : kind === "outbound" ? "去程" : "返程"}已标记为自行安排`,
  );
}

async function chooseHotel(hotel: HotelOption): Promise<void> {
  await saveSelection(
    { ...selection.value, hotel_id: hotel.hotel_id, self_arranged_hotel: false },
    hotel.hotel_id,
    "住宿已选择",
  );
}

async function saveSelection(
  next: PlanningSelection,
  target: string,
  message: string,
): Promise<void> {
  if (savingSelection.value) return;
  savingSelection.value = target;
  selectionNotice.value = "";
  try {
    session.value = await updatePlanningSelection(sessionId, next);
    error.value = "";
    selectionNotice.value = message;
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    savingSelection.value = null;
  }
}

async function loadTransfers(direction: RailDirection): Promise<void> {
  loadingTransfers.value = direction;
  try {
    const options = await loadRailTransfers(sessionId, direction);
    if (session.value) {
      const field = direction === "outbound" ? "outbound_transfers" : "return_transfers";
      session.value = { ...session.value, [field]: options };
    }
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    loadingTransfers.value = null;
  }
}

async function openHotel(hotel: HotelOption): Promise<void> {
  detail.value = null;
  loadingDetail.value = true;
  try {
    detail.value = await getHotelDetail(sessionId, hotel.hotel_id);
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    loadingDetail.value = false;
  }
}

async function generate(): Promise<void> {
  generating.value = true;
  try {
    session.value = await generatePlanningSession(sessionId);
    startPolling();
  } catch (requestError) {
    error.value = errorMessage(requestError);
    generating.value = false;
  }
}

async function retry(): Promise<void> {
  session.value = await retryPlanningSession(sessionId);
  startPolling();
}

function selectedRail(direction: RailDirection): RailOption | undefined {
  const choice = direction === "outbound" ? selection.value.outbound : selection.value.return_trip;
  return allRail(direction).find((item) => item.option_id === choice?.option_id);
}

function duration(minutes: number): string {
  return `${Math.floor(minutes / 60)}时${minutes % 60 ? `${minutes % 60}分` : ""}`;
}

function money(value: number | null): string {
  return value === null ? "价格待确认" : `¥${Math.round(value).toLocaleString("zh-CN")}`;
}

function stepClass(status: string): string {
  return `step-status ${status}`;
}

function hotelImageVisible(hotel: HotelOption): boolean {
  return Boolean(hotel.image_url) && !failedHotelImages.value.has(hotel.hotel_id);
}

function hideHotelImage(hotelId: string): void {
  failedHotelImages.value = new Set(failedHotelImages.value).add(hotelId);
}

function hotelSourceLabel(source: HotelOption["source"], detailView = false): string {
  const labels = {
    rollinggo: detailView ? "RollingGo 实时详情" : "RollingGo 实时",
    dida: detailView ? "DIDA 实时详情" : "DIDA 实时",
    osm: detailView ? "OSM 静态详情" : "OSM 静态",
  };
  return labels[source];
}

function hotelProviderLabel(status: string): string {
  const labels: Record<string, string> = {
    rollinggo_oauth: "RollingGo 已登录",
    login_required: "RollingGo 未登录，当前使用本地候选",
    dida_token: "DIDA 已配置",
    unknown: "住宿登录状态未知",
    loading: "正在检查住宿登录状态",
  };
  return labels[status] || "住宿服务状态未知";
}
</script>

<template>
  <section class="workspace-page session-workspace">
    <header class="workspace-heading">
      <div><p class="section-kicker">旅行数据发现</p><h1>{{ session?.request.origin || "-" }} → {{ session?.request.destination || "-" }}</h1><p v-if="session">{{ session.request.start_date }} 至 {{ session.request.end_date }} · {{ session.request.travelers }} 人</p></div>
      <ol class="stepper" aria-label="规划步骤"><li><span>1</span>需求</li><li class="active"><span>2</span>选择</li><li><span>3</span>行程</li></ol>
    </header>

    <p v-if="error" class="error-message global-error" role="alert">{{ error }}</p>
    <p v-if="selectionNotice" class="success-message global-error" role="status">{{ selectionNotice }}</p>
    <section v-if="!session" class="loading-panel"><LoaderCircle class="spin" :size="22" />正在恢复规划会话…</section>
    <template v-else>
      <section class="progress-strip" aria-label="查询进度">
        <article v-for="step in session.steps.slice(0, 5)" :key="step.name" :class="stepClass(step.status)">
          <span class="status-dot"><LoaderCircle v-if="step.status === 'running'" class="spin" :size="15" /><Check v-else-if="step.status === 'completed'" :size="15" /><CircleAlert v-else-if="['degraded','failed'].includes(step.status)" :size="15" /></span>
          <div><strong>{{ step.label }}</strong><small>{{ step.message || (step.duration_ms !== null ? `${step.duration_ms} ms` : statusText(step.status)) }}</small></div>
        </article>
      </section>

      <div v-if="session.status === 'failed'" class="action-notice"><CircleAlert :size="20" /><div><strong>查询未完成</strong><p>{{ session.error_message }}</p></div><button class="secondary-button" @click="retry"><RefreshCw :size="16" />重试</button></div>

      <section class="selection-section rail-section">
        <header class="section-toolbar"><div><TrainFront :size="20" /><span><strong>往返车次</strong><small>数据来自 12306 MCP，仅提供查询与跳转</small></span></div><div class="segmented-control"><button :class="{ active: activeRailTab === 'outbound' }" @click="activeRailTab = 'outbound'">去程</button><button :class="{ active: activeRailTab === 'return' }" @click="activeRailTab = 'return'">返程</button></div></header>
        <div class="filter-bar"><select v-model="trainType" aria-label="车次类型"><option value="all">全部车次</option><option value="G">高铁 G</option><option value="D">动车 D</option><option value="C">城际 C</option><option value="K">快速 K</option></select><select v-model="departurePeriod" aria-label="出发时段"><option value="all">全部时段</option><option value="morning">上午</option><option value="afternoon">下午</option><option value="evening">晚间</option></select><select v-model="sortBy" aria-label="排序"><option value="departure">按出发时间</option><option value="duration">按历时</option><option value="price">按价格</option></select><label class="checkbox-control"><input v-model="availableOnly" type="checkbox" />仅看有票</label></div>
        <div v-if="!discoveryReady && !railOptions.length" class="skeleton-list"><div v-for="item in 3" :key="item" class="skeleton-row" /></div>
        <div v-else-if="railOptions.length" class="rail-table-wrap">
          <table class="rail-table">
            <thead><tr><th>车次</th><th>时间 / 车站</th><th>历时</th><th>席别与余票</th><th>参考价</th><th><span class="sr-only">选择</span></th></tr></thead>
            <tbody>
              <tr v-for="option in railOptions" :key="option.option_id" :class="{ selected: selectedRail(activeRailTab)?.option_id === option.option_id }">
                <td><strong>{{ option.train_code }}</strong><small>{{ option.train_type }}<template v-if="option.is_transfer"> · {{ option.transfer_station }} 中转</template></small></td>
                <td><span class="time-pair"><strong>{{ option.departure_time }}</strong><ArrowRight :size="14" /><strong>{{ option.arrival_time }}</strong></span><small>{{ option.from_station }} → {{ option.to_station }}</small></td>
                <td>{{ duration(option.duration_minutes) }}</td>
                <td><select v-if="option.seats.length" v-model="selectedSeats[option.option_id]" :aria-label="`${option.train_code}席别`" :disabled="savingSelection !== null"><option v-for="seat in option.seats" :key="seat.name" :value="seat.name">{{ seat.name }} · {{ seat.availability }}<template v-if="seat.price !== null"> · {{ money(seat.price) }}</template></option></select><span v-else class="muted-text">余票未知</span></td>
                <td class="price-cell">{{ money(option.price_from) }}</td>
                <td><button class="select-button" type="button" :disabled="savingSelection !== null" @click="chooseRail(activeRailTab, option)"><LoaderCircle v-if="savingSelection === option.option_id" class="spin" :size="16" /><Check v-else :size="16" />{{ savingSelection === option.option_id ? "保存中…" : selectedRail(activeRailTab)?.option_id === option.option_id ? "已选择" : "选择" }}</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-inline"><TrainFront :size="24" /><span><strong>暂无直达车次</strong><small>可以查询一次中转，或标记为自行安排。</small></span></div>
        <footer class="section-footer"><button class="text-action" :disabled="loadingTransfers === activeRailTab" @click="loadTransfers(activeRailTab)"><RefreshCw :class="{ spin: loadingTransfers === activeRailTab }" :size="16" />{{ loadingTransfers === activeRailTab ? "查询中转中…" : "查询中转方案" }}</button><button class="text-action" @click="chooseSelfArranged(activeRailTab)">自行安排{{ activeRailTab === "outbound" ? "去程" : "返程" }}</button></footer>
      </section>

      <section v-if="!oneDayTrip" class="selection-section hotel-section">
        <header class="section-toolbar"><div><BedDouble :size="20" /><span><strong>住宿选择</strong><small>RollingGo 实时房价优先，不可用时展示本地 OSM 候选</small><small class="provider-status">{{ hotelProviderLabel(hotelProviderStatus) }}</small></span></div><span v-if="selectedHotel" class="selection-summary"><Check :size="15" />已选 {{ selectedHotel.name }}</span></header>
        <div v-if="!discoveryReady && !session.hotel_options.length" class="hotel-list"><div v-for="item in 3" :key="item" class="skeleton-hotel" /></div>
        <div v-else-if="session.hotel_options.length" class="hotel-list"><article v-for="hotelItem in session.hotel_options" :key="hotelItem.hotel_id" :class="['hotel-row', { selected: selection.hotel_id === hotelItem.hotel_id }]"><div class="hotel-thumb"><img v-if="hotelImageVisible(hotelItem)" :src="hotelItem.image_url || ''" :alt="`${hotelItem.name}图片`" loading="lazy" @error="hideHotelImage(hotelItem.hotel_id)" /><Hotel v-else :size="24" /><span v-if="!hotelImageVisible(hotelItem)" class="sr-only">暂无酒店图片</span></div><div class="hotel-main"><div><h3>{{ hotelItem.name }}</h3><p><MapPin :size="14" />{{ hotelItem.address || "地址待补充" }}</p></div><div class="hotel-meta"><span v-if="hotelItem.star_rating">{{ hotelItem.star_rating }} 星</span><span v-if="hotelItem.distance_km !== null">距中心 {{ hotelItem.distance_km }} km</span><span>{{ hotelSourceLabel(hotelItem.source) }}</span></div><div v-if="hotelItem.facilities.length" class="facility-list"><span v-for="item in hotelItem.facilities.slice(0, 4)" :key="item">{{ item }}</span></div></div><div class="hotel-price"><strong>{{ money(hotelItem.price_per_night) }}</strong><small v-if="hotelItem.price_per_night !== null">每晚参考</small><div><button class="icon-button" title="查看酒店详情" aria-label="查看酒店详情" @click="openHotel(hotelItem)"><ExternalLink :size="17" /></button><button class="select-button" @click="chooseHotel(hotelItem)"><Check :size="16" />选择</button></div></div></article></div>
        <div v-else class="empty-inline"><BedDouble :size="24" /><span><strong>暂无酒店候选</strong><small>可以标记为自行安排，不会阻断行程。</small></span></div>
        <footer class="section-footer"><button class="text-action" @click="chooseSelfArranged('hotel')">住宿自行安排</button></footer>
      </section>
      <section v-else class="selection-section one-day-notice">
        <BedDouble :size="20" />
        <div><strong>一日行程无需选择住宿</strong><p>系统不会查询酒店，也不会计算住宿费用。</p></div>
      </section>

      <section class="weather-summary"><CloudSun :size="20" /><div><strong>天气预报</strong><span v-for="item in session.weather" :key="item.date">{{ item.date.slice(5) }} {{ item.warning || item.day_weather || "未知" }} {{ item.day_temperature ? `${item.day_temperature}℃` : "" }}</span></div></section>

      <footer class="selection-footer"><div><strong>{{ canGenerate ? "选择已完整" : "还需确认往返交通与住宿" }}</strong><p>生成采用确定性规划，通常 2 秒内完成。</p></div><button class="primary-button" :disabled="!canGenerate || generating || session.status === 'generating'" @click="generate"><LoaderCircle v-if="generating || session.status === 'generating'" class="spin" :size="18" /><ArrowRight v-else :size="18" />{{ generating || session.status === "generating" ? "正在生成行程…" : "生成可编辑行程" }}</button></footer>
    </template>

    <div v-if="loadingDetail || detail" class="drawer-backdrop" @click.self="detail = null"><aside class="detail-drawer" aria-label="酒店详情"><button class="icon-button drawer-close" aria-label="关闭酒店详情" @click="detail = null"><X :size="20" /></button><div v-if="loadingDetail" class="loading-panel"><LoaderCircle class="spin" :size="22" />正在读取房型与退改规则…</div><template v-else-if="detail"><span class="source-tag">{{ hotelSourceLabel(detail.source, true) }}</span><h2>{{ detail.name }}</h2><p class="muted-text">{{ detail.address }}</p><p v-if="detail.description">{{ detail.description }}</p><div v-if="detail.facilities.length" class="facility-list"><span v-for="item in detail.facilities" :key="item">{{ item }}</span></div><h3>可用房型</h3><div v-if="detail.rooms.length" class="room-list"><article v-for="room in detail.rooms" :key="room.room_id"><div><strong>{{ room.name }}</strong><small>{{ room.breakfast || "早餐信息待确认" }} · {{ room.cancellation || "退改规则待确认" }}</small></div><span>{{ money(room.price) }}</span></article></div><p v-else class="empty-copy">当前数据源没有返回房型，请前往供应商确认。</p><a v-if="detail.booking_url" class="primary-button" :href="detail.booking_url" target="_blank" rel="noreferrer"><ExternalLink :size="17" />前往供应商</a></template></aside></div>
  </section>
</template>

<script lang="ts">
function statusText(status: string): string {
  const labels: Record<string, string> = { pending: "等待中", running: "查询中", completed: "已完成", degraded: "已降级", failed: "失败", cancelled: "已取消" };
  return labels[status] || status;
}
</script>
