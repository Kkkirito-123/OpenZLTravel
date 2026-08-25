<script setup lang="ts">
import {
  BedDouble,
  Check,
  CloudSun,
  ImageOff,
  MapPin,
  TrainFront,
  WalletCards,
} from "@lucide/vue";
import { ref, watch } from "vue";

import type { AssistantAction, AssistantSnapshot, RailOption } from "../../types";

const props = defineProps<{ snapshot: AssistantSnapshot; busy: boolean }>();
const emit = defineEmits<{ select: [action: AssistantAction] }>();
const attractionDraft = ref<string[]>([]);
const failedImages = ref(new Set<string>());

watch(
  () => props.snapshot.selection.attraction_ids,
  (value) => { attractionDraft.value = [...(value ?? [])]; },
  { immediate: true },
);

function toggleAttraction(id: string): void {
  attractionDraft.value = attractionDraft.value.includes(id)
    ? attractionDraft.value.filter((item) => item !== id)
    : [...attractionDraft.value, id];
}

function chooseRail(kind: "select_outbound" | "select_return", option: RailOption): void {
  emit("select", {
    kind,
    option_id: option.option_id,
    ...(option.seats?.[0]?.name ? { seat_type: option.seats[0].name } : {}),
  });
}

function money(value?: number | null): string {
  return value === null || value === undefined
    ? "价格待确认"
    : new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: "CNY",
      maximumFractionDigits: 0,
    }).format(value);
}

function imageKey(kind: "poi" | "hotel", id: string, url?: string | null): string {
  return `${kind}:${id}:${url || ""}`;
}

function imageAvailable(kind: "poi" | "hotel", id: string, url?: string | null): boolean {
  return Boolean(url && !failedImages.value.has(imageKey(kind, id, url)));
}

function markImageFailed(kind: "poi" | "hotel", id: string, url?: string | null): void {
  if (!url) return;
  failedImages.value = new Set(failedImages.value).add(imageKey(kind, id, url));
}
</script>

<template>
  <div class="assistant-card-stack">
    <section v-if="snapshot.destination_candidates.length && !snapshot.requirements.destination" class="assistant-card-section">
      <header><MapPin :size="18" /><div><strong>目的地建议</strong><p>都来自当前地点目录，可继续比较再选择。</p></div></header>
      <div class="choice-grid destination-grid">
        <button
          v-for="candidate in snapshot.destination_candidates"
          :key="candidate.candidate_id"
          class="choice-card"
          type="button"
          :disabled="busy"
          @click="emit('select', { kind: 'select_destination', candidate_id: candidate.candidate_id })"
        >
          <span class="choice-title"><strong>{{ candidate.city.name }}</strong><small>{{ Math.round(candidate.score * 100) }} 分</small></span>
          <p>{{ candidate.reasons.join(' · ') }}</p>
          <small>{{ candidate.attraction_count || 0 }} 景点 · {{ candidate.hotel_count || 0 }} 酒店</small>
        </button>
      </div>
    </section>

    <section v-if="snapshot.facts.catalog?.attractions.length" class="assistant-card-section">
      <header><MapPin :size="18" /><div><strong>景点卡片</strong><p>勾选后由服务端校验真实 POI ID。</p></div></header>
      <div class="choice-grid poi-grid">
        <button
          v-for="poi in snapshot.facts.catalog.attractions"
          :key="poi.id"
          :class="['choice-card', { selected: attractionDraft.includes(poi.id) }]"
          type="button"
          :disabled="busy"
          @click="toggleAttraction(poi.id)"
        >
          <img
            v-if="imageAvailable('poi', poi.id, poi.image_url)"
            :src="poi.image_url || undefined"
            :alt="poi.name"
            width="76"
            height="68"
            loading="lazy"
            decoding="async"
            referrerpolicy="no-referrer"
            @error="markImageFailed('poi', poi.id, poi.image_url)"
          />
          <span v-else class="poi-placeholder media-placeholder"><ImageOff :size="18" /><small>暂无图片</small></span>
          <span class="choice-copy"><strong>{{ poi.name }}</strong><small>{{ poi.address || poi.type_name || '地址以 Provider 为准' }}</small></span>
          <Check v-if="attractionDraft.includes(poi.id)" class="selected-check" :size="17" />
        </button>
      </div>
      <button
        class="primary-button compact-action"
        type="button"
        :disabled="busy || !attractionDraft.length"
        @click="emit('select', { kind: 'select_attractions', attraction_ids: attractionDraft })"
      >确认 {{ attractionDraft.length }} 个景点</button>
    </section>

    <section v-if="snapshot.facts.outbound_options.length" class="assistant-card-section">
      <header><TrainFront :size="18" /><div><strong>去程车次</strong><p>选择一项，或明确自行安排。</p></div></header>
      <div class="choice-grid fact-grid">
        <button
          v-for="option in snapshot.facts.outbound_options"
          :key="option.option_id"
          :class="['choice-card', { selected: snapshot.selection.outbound?.option_id === option.option_id }]"
          type="button"
          :disabled="busy"
          @click="chooseRail('select_outbound', option)"
        >
          <span class="choice-title"><strong>{{ option.train_code }}</strong><b>{{ money(option.price_from) }}</b></span>
          <p>{{ option.from_station }} {{ option.departure_time }} → {{ option.to_station }} {{ option.arrival_time }}</p>
          <small>{{ option.seats?.[0]?.name || '席别待确认' }} · {{ option.seats?.[0]?.availability || (option.has_ticket ? '有票' : '库存待确认') }}</small>
        </button>
      </div>
      <button class="text-button" type="button" :disabled="busy" @click="emit('select', { kind: 'self_arrange', target: 'outbound' })">去程由我自行安排</button>
    </section>

    <section v-if="snapshot.facts.return_options.length" class="assistant-card-section">
      <header><TrainFront :size="18" /><div><strong>返程车次</strong><p>Assistant 提交前会再次刷新库存。</p></div></header>
      <div class="choice-grid fact-grid">
        <button
          v-for="option in snapshot.facts.return_options"
          :key="option.option_id"
          :class="['choice-card', { selected: snapshot.selection.return_trip?.option_id === option.option_id }]"
          type="button"
          :disabled="busy"
          @click="chooseRail('select_return', option)"
        >
          <span class="choice-title"><strong>{{ option.train_code }}</strong><b>{{ money(option.price_from) }}</b></span>
          <p>{{ option.from_station }} {{ option.departure_time }} → {{ option.to_station }} {{ option.arrival_time }}</p>
          <small>{{ option.seats?.[0]?.name || '席别待确认' }} · {{ option.seats?.[0]?.availability || (option.has_ticket ? '有票' : '库存待确认') }}</small>
        </button>
      </div>
      <button class="text-button" type="button" :disabled="busy" @click="emit('select', { kind: 'self_arrange', target: 'return' })">返程由我自行安排</button>
    </section>

    <section v-if="snapshot.facts.hotel_options.length" class="assistant-card-section">
      <header><BedDouble :size="18" /><div><strong>酒店</strong><p>价格来自当前酒店 Provider 或明确标记的降级结果。</p></div></header>
      <div class="choice-grid hotel-grid">
        <button
          v-for="hotel in snapshot.facts.hotel_options"
          :key="hotel.hotel_id"
          :class="['choice-card', { selected: snapshot.selection.hotel_id === hotel.hotel_id }]"
          type="button"
          :disabled="busy"
          @click="emit('select', { kind: 'select_hotel', hotel_id: hotel.hotel_id })"
        >
          <img
            v-if="imageAvailable('hotel', hotel.hotel_id, hotel.image_url)"
            :src="hotel.image_url || undefined"
            :alt="hotel.name"
            width="76"
            height="68"
            loading="lazy"
            decoding="async"
            referrerpolicy="no-referrer"
            @error="markImageFailed('hotel', hotel.hotel_id, hotel.image_url)"
          />
          <span v-else class="poi-placeholder media-placeholder"><ImageOff :size="18" /><small>暂无图片</small></span>
          <span class="choice-copy"><strong>{{ hotel.name }}</strong><small>{{ hotel.address || hotel.source || '住宿信息' }}</small><b>{{ money(hotel.total_price ?? hotel.price_per_night) }}</b></span>
        </button>
      </div>
      <button class="text-button" type="button" :disabled="busy" @click="emit('select', { kind: 'self_arrange', target: 'hotel' })">住宿由我自行安排</button>
    </section>

    <section v-if="snapshot.facts.weather.length" class="assistant-card-section weather-section">
      <header><CloudSun :size="18" /><div><strong>天气事实</strong><p>最终提交前会重新查询。</p></div></header>
      <div class="weather-grid">
        <article v-for="day in snapshot.facts.weather" :key="day.date">
          <strong>{{ day.date }}</strong>
          <span>{{ day.day_weather || day.warning || '天气未知' }}</span>
          <small>{{ day.day_temperature ? `${day.day_temperature}℃` : '温度未知' }}</small>
        </article>
      </div>
    </section>

    <section v-if="snapshot.status === 'ready'" class="ready-order-card">
      <WalletCards :size="20" />
      <div><strong>资料与选择已齐全</strong><p>可以继续自然语言修改，或回复“开始规划”刷新事实并签发工单。</p></div>
    </section>
  </div>
</template>
