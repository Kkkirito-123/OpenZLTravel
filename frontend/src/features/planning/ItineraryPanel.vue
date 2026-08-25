<script setup lang="ts">
import {
  ArrowLeft,
  BedDouble,
  CalendarDays,
  CloudSun,
  Clock3,
  ExternalLink,
  ImageOff,
  Lightbulb,
  MapPin,
  Route,
  TrainFront,
  Utensils,
  WalletCards,
} from "@lucide/vue";
import { computed, ref } from "vue";

import type {
  ActivityDraft,
  DayDraft,
  PlaceSnapshot,
  RailChoice,
  RailOption,
  TripRecord,
  WeatherDay,
} from "../../types";

const props = defineProps<{ trip: TripRecord; historical?: boolean }>();
defineEmits<{ back: [] }>();
const failedImages = ref(new Set<string>());

const destination = computed(() => props.trip.city?.name
  ?? props.trip.requirements.destination
  ?? props.trip.requirements.region
  ?? "旅行目的地");
const budgetRows = computed(() => {
  const budget = props.trip.budget;
  if (!budget) return [];
  return [
    ["城际交通", budget.intercity_transport],
    ["市内交通", budget.local_transport],
    ["住宿", budget.hotel],
    ["餐饮估算", budget.meals_estimated],
    ["门票估算", budget.tickets_estimated],
  ] as Array<[string, number | null | undefined]>;
});
const knownTotal = computed(() => props.trip.budget?.total_known);
const railRows = computed(() => [
  {
    label: "去程",
    option: props.trip.outbound_rail,
    choice: props.trip.selection?.outbound,
    selfArranged: props.trip.selection?.self_arranged_outbound,
  },
  {
    label: "返程",
    option: props.trip.return_rail,
    choice: props.trip.selection?.return_trip,
    selfArranged: props.trip.selection?.self_arranged_return,
  },
].filter((item) => item.option || item.selfArranged));
const recommendationImages = computed(() => {
  const index = props.trip.place_index ?? {};
  const referenced = new Set<string>();
  for (const day of props.trip.draft.days) {
    for (const activity of day.activities) referenced.add(activity.poi_id);
    for (const mealId of day.meal_ids ?? []) referenced.add(mealId);
    if (day.hotel_id) referenced.add(day.hotel_id);
  }
  return [...referenced]
    .map((id) => index[id])
    .filter((place): place is PlaceSnapshot => Boolean(place && imageUrl(place.image_url)))
    .slice(0, 8);
});

function dayDate(day: DayDraft): string {
  const start = props.trip.requirements.start_date;
  if (!start) return `第 ${day.day_index} 天`;
  const date = new Date(`${start}T00:00:00`);
  date.setDate(date.getDate() + day.day_index - 1);
  return new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" }).format(date);
}

function weatherFor(day: DayDraft): WeatherDay | undefined {
  const date = dateAtIndex(day.day_index);
  return props.trip.weather?.find((item) => item.date === date);
}

function dateAtIndex(index: number): string | null {
  const start = props.trip.requirements.start_date;
  if (!start) return null;
  const [year, month, day] = start.split("-").map(Number);
  if (!year || !month || !day) return null;
  const date = new Date(Date.UTC(year, month - 1, day));
  date.setUTCDate(date.getUTCDate() + index - 1);
  return date.toISOString().slice(0, 10);
}

function activityName(activity: ActivityDraft): string {
  return props.trip.place_index?.[activity.poi_id]?.name || activity.poi_id;
}

function activityPlace(activity: ActivityDraft): PlaceSnapshot | undefined {
  return props.trip.place_index?.[activity.poi_id];
}

function activityLocation(activity: ActivityDraft): string {
  const place = activityPlace(activity);
  if (!place) return "";
  if (place.address) return place.address;
  if (place.latitude !== null && place.latitude !== undefined
    && place.longitude !== null && place.longitude !== undefined) {
    return `地址未收录 · 坐标 ${place.latitude.toFixed(4)}, ${place.longitude.toFixed(4)}`;
  }
  return "地址待确认";
}

function mapUrl(place?: PlaceSnapshot): string | null {
  if (place?.latitude === null || place?.latitude === undefined
    || place.longitude === null || place.longitude === undefined) return null;
  return `https://uri.amap.com/marker?position=${place.longitude},${place.latitude}&name=${encodeURIComponent(place.name)}`;
}

function imageUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function imageSource(value?: string | null): string {
  const url = imageUrl(value);
  return url ? new URL(url).hostname.replace(/^www\./, "") : "来源未标注";
}

function imageKey(place: PlaceSnapshot): string {
  return `${place.fact_id}|${imageUrl(place.image_url) ?? ""}`;
}

function hideImage(place: PlaceSnapshot): void {
  failedImages.value = new Set(failedImages.value).add(imageKey(place));
}

function mealText(day: DayDraft): string {
  return (day.meal_ids ?? []).map((id) => props.trip.place_index?.[id]?.name || id).join("、");
}

function hotelText(day: DayDraft): string {
  const id = day.hotel_id;
  return (id ? props.trip.place_index?.[id]?.name : null) || id || "";
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "待确认";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    maximumFractionDigits: 0,
  }).format(value);
}

function weatherText(weather?: WeatherDay): string {
  if (!weather) return "天气未知";
  const condition = weather.warning || weather.day_weather || "天气未知";
  const temperature = weather.day_temperature ? ` · ${weather.day_temperature}℃` : "";
  return `${condition}${temperature}`;
}

function railSeat(option: RailOption, choice?: RailChoice | null): string {
  return choice?.seat_type || option.seats?.[0]?.name || "席别待确认";
}

function railPrice(option: RailOption, choice?: RailChoice | null): number | null | undefined {
  const selected = option.seats?.find((seat) => seat.name === choice?.seat_type);
  return selected?.price ?? option.price_from;
}

function externalUrl(value?: string | null): string | null {
  return imageUrl(value);
}

function dayRoutes(dayIndex: number): unknown[] {
  return props.trip.routes?.[String(dayIndex)] ?? props.trip.routes?.[dayIndex] ?? [];
}
</script>

<template>
  <section class="itinerary-panel" aria-labelledby="itinerary-title">
    <header class="itinerary-header">
      <button
        v-if="historical"
        class="text-button back-button"
        type="button"
        @click="$emit('back')"
      >
        <ArrowLeft :size="16" />返回当前行程
      </button>
      <p class="section-kicker">{{ historical ? "历史行程" : "已生成行程" }}</p>
      <h2 id="itinerary-title">{{ destination }}</h2>
      <p>{{ trip.draft.summary }}</p>
      <div class="trip-meta">
        <span><CalendarDays :size="15" />{{ trip.requirements.start_date || "日期待确认" }}<template v-if="trip.requirements.end_date"> — {{ trip.requirements.end_date }}</template></span>
        <span><WalletCards :size="15" />已知总额 {{ money(knownTotal) }}</span>
      </div>
    </header>

    <section
      v-if="recommendationImages.length"
      class="recommendation-gallery"
      aria-labelledby="recommendation-gallery-title"
    >
      <header class="recommendation-gallery-heading">
        <div>
          <p class="section-kicker">推荐图片</p>
          <h3 id="recommendation-gallery-title">行程中的真实地点</h3>
        </div>
        <span>{{ recommendationImages.length }} 张</span>
      </header>
      <div class="recommendation-image-grid">
        <figure v-for="place in recommendationImages" :key="place.fact_id">
          <img
            v-if="!failedImages.has(imageKey(place))"
            class="recommendation-image"
            :src="imageUrl(place.image_url) || undefined"
            :alt="`${place.name}推荐图片`"
            width="520"
            height="320"
            loading="lazy"
            decoding="async"
            referrerpolicy="no-referrer"
            @error="hideImage(place)"
          />
          <div v-else class="recommendation-image-fallback" role="status">
            <ImageOff :size="22" />
            <span>推荐图片加载失败</span>
          </div>
          <figcaption>
            <strong>{{ place.name }}</strong>
            <a
              :href="imageUrl(place.image_url) || undefined"
              target="_blank"
              rel="noreferrer"
            ><ExternalLink :size="12" />图片来源：{{ imageSource(place.image_url) }}</a>
          </figcaption>
        </figure>
      </div>
    </section>

    <section v-if="railRows.length" class="transport-panel" aria-labelledby="transport-title">
      <div class="budget-heading"><TrainFront :size="18" /><h3 id="transport-title">城际交通</h3></div>
      <div class="transport-grid">
        <article v-for="row in railRows" :key="row.label" class="transport-card">
          <span class="transport-label">{{ row.label }}</span>
          <template v-if="row.option">
            <div class="transport-title-row">
              <strong>{{ row.option.train_code }}</strong>
              <span>{{ money(railPrice(row.option, row.choice)) }}</span>
            </div>
            <p>{{ row.option.from_station }} → {{ row.option.to_station }}</p>
            <p>{{ row.option.travel_date || "日期待确认" }} · {{ row.option.departure_time }} — {{ row.option.arrival_time }}</p>
            <p>{{ railSeat(row.option, row.choice) }}</p>
            <a
              v-if="externalUrl(row.option.booking_url)"
              :href="externalUrl(row.option.booking_url) || undefined"
              target="_blank"
              rel="noreferrer"
            ><ExternalLink :size="12" />前往官方渠道确认</a>
          </template>
          <p v-else class="self-arranged-transport">由用户自行安排</p>
        </article>
      </div>
    </section>

    <div class="itinerary-days">
      <article v-for="day in trip.draft.days" :key="day.day_index" class="day-card">
        <header class="day-heading">
          <span class="day-index">D{{ day.day_index }}</span>
          <div><h3>{{ day.theme }}</h3><p>{{ dayDate(day) }}</p></div>
          <span class="day-weather"><CloudSun :size="15" />{{ weatherText(weatherFor(day)) }}</span>
        </header>

        <ol class="activity-list">
          <li v-for="activity in day.activities" :key="`${day.day_index}-${activity.poi_id}`">
            <time>{{ activity.start_time }}</time>
            <span class="timeline-dot" aria-hidden="true" />
            <div>
              <strong>{{ activityName(activity) }}</strong>
              <p v-if="activityPlace(activity)" class="activity-location">
                <MapPin :size="13" />{{ activityLocation(activity) }}
                <a
                  v-if="mapUrl(activityPlace(activity))"
                  :href="mapUrl(activityPlace(activity)) || undefined"
                  target="_blank"
                  rel="noreferrer"
                  @click.stop
                ><ExternalLink :size="12" />地图</a>
              </p>
              <p><Clock3 :size="13" />停留约 {{ activity.duration_minutes }} 分钟<template v-if="activity.note"> · {{ activity.note }}</template></p>
            </div>
          </li>
        </ol>

        <div v-if="day.meal_ids?.length" class="day-detail-row">
          <Utensils :size="16" />
          <span>餐饮：{{ mealText(day) }}</span>
        </div>
        <div v-if="hotelText(day)" class="day-detail-row">
          <BedDouble :size="16" />
          <span>住宿：{{ hotelText(day) }}</span>
        </div>
        <div v-if="dayRoutes(day.day_index).length" class="day-detail-row">
          <Route :size="16" />
          <span>{{ dayRoutes(day.day_index).length }} 段已校验路线</span>
        </div>
        <ul v-if="day.notes?.length" class="day-notes">
          <li v-for="note in day.notes" :key="note">{{ note }}</li>
        </ul>
      </article>
    </div>

    <section v-if="budgetRows.length" class="budget-panel" aria-labelledby="budget-title">
      <div class="budget-heading"><WalletCards :size="18" /><h3 id="budget-title">预算明细</h3></div>
      <dl>
        <div v-for="row in budgetRows" :key="row[0]"><dt>{{ row[0] }}</dt><dd>{{ money(row[1]) }}</dd></div>
        <div class="budget-total"><dt>已知总额</dt><dd>{{ money(knownTotal) }}</dd></div>
      </dl>
    </section>

    <section v-if="trip.draft.tips?.length" class="tips-panel" aria-labelledby="tips-title">
      <div class="budget-heading"><Lightbulb :size="18" /><h3 id="tips-title">出行提示</h3></div>
      <ul><li v-for="tip in trip.draft.tips" :key="tip">{{ tip }}</li></ul>
    </section>
  </section>
</template>
