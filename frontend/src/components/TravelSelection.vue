<script setup lang="ts">
import {
  ArrowRight,
  BedDouble,
  Check,
  Clock3,
  ExternalLink,
  Hotel,
  ImageOff,
  LoaderCircle,
  MapPin,
  TrainFront,
} from "@lucide/vue";
import { computed, reactive, ref } from "vue";

import type { HotelOption, RailOption, TravelSelection, TravelSelectionInterrupt } from "../types";

const props = defineProps<{
  interrupt: TravelSelectionInterrupt;
  existing?: TravelSelection;
  busy: boolean;
}>();
const emit = defineEmits<{ select: [selection: TravelSelection] }>();

const selfValue = "__self_arranged__";
const outboundId = ref(
  props.existing?.self_arranged_outbound ? selfValue : props.existing?.outbound?.option_id ?? "",
);
const returnId = ref(
  props.existing?.self_arranged_return ? selfValue : props.existing?.return_trip?.option_id ?? "",
);
const hotelId = ref(
  props.existing?.self_arranged_hotel ? selfValue : props.existing?.hotel_id ?? "",
);
const seatTypes = reactive<Record<string, string>>({
  ...(props.existing?.outbound?.option_id
    ? { [props.existing.outbound.option_id]: props.existing.outbound.seat_type ?? "" }
    : {}),
  ...(props.existing?.return_trip?.option_id
    ? { [props.existing.return_trip.option_id]: props.existing.return_trip.seat_type ?? "" }
    : {}),
});
const failedImages = ref(new Set<string>());
const requiresHotel = computed(() => props.interrupt.requires_hotel);
const allowSelfArranged = computed(() => props.interrupt.self_arranged_allowed);
const complete = computed(() => Boolean(outboundId.value)
  && Boolean(returnId.value)
  && (!requiresHotel.value || Boolean(hotelId.value)));

function submit(): void {
  if (!complete.value) return;
  emit("select", {
    outbound: railChoice(outboundId.value),
    return_trip: railChoice(returnId.value),
    hotel_id: requiresHotel.value && hotelId.value !== selfValue ? hotelId.value : null,
    self_arranged_outbound: outboundId.value === selfValue,
    self_arranged_return: returnId.value === selfValue,
    self_arranged_hotel: requiresHotel.value && hotelId.value === selfValue,
  });
}

function railChoice(optionId: string) {
  if (!optionId || optionId === selfValue) return null;
  return { option_id: optionId, seat_type: seatTypes[optionId] || null };
}

function defaultSeat(option: RailOption): string {
  return seatTypes[option.option_id] || option.seats?.[0]?.name || "";
}

function chooseRail(target: "outbound" | "return", option: RailOption): void {
  if (target === "outbound") outboundId.value = option.option_id;
  else returnId.value = option.option_id;
  if (!seatTypes[option.option_id]) seatTypes[option.option_id] = defaultSeat(option);
}

function duration(minutes = 0): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!hours) return `${rest} 分钟`;
  return `${hours} 小时${rest ? ` ${rest} 分` : ""}`;
}

function money(value?: number | null): string {
  if (value === null || value === undefined) return "价格待确认";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    maximumFractionDigits: 0,
  }).format(value);
}

function hideImage(hotelIdValue: string): void {
  failedImages.value = new Set(failedImages.value).add(hotelIdValue);
}

function locationText(item: HotelOption): string {
  if (item.address) return item.address;
  if (item.latitude !== null && item.latitude !== undefined
    && item.longitude !== null && item.longitude !== undefined) {
    return `地址未收录 · 坐标 ${item.latitude.toFixed(4)}, ${item.longitude.toFixed(4)}`;
  }
  return "地址待确认";
}

function mapUrl(item: HotelOption): string | null {
  if (item.latitude === null || item.latitude === undefined
    || item.longitude === null || item.longitude === undefined) return null;
  const query = encodeURIComponent(item.name);
  return `https://uri.amap.com/marker?position=${item.longitude},${item.latitude}&name=${query}`;
}
</script>

<template>
  <section class="interrupt-card travel-selection-card" aria-labelledby="travel-selection-title">
    <header>
      <span class="interrupt-icon"><TrainFront :size="20" /></span>
      <div>
        <p class="section-kicker">交通与住宿</p>
        <h3 id="travel-selection-title">选择真实候选后生成行程</h3>
        <p>价格未知会保持未知；自行安排不会阻断后续规划。</p>
      </div>
    </header>
    <p v-if="interrupt.error" class="interrupt-error" role="alert">{{ interrupt.error.message }}</p>

    <section class="selection-group" aria-labelledby="outbound-title">
      <div class="selection-group-heading">
        <TrainFront :size="18" />
        <div><h4 id="outbound-title">去程车次</h4><p>选择席别后按真实报价计入预算</p></div>
      </div>
      <div class="rail-option-list">
        <article
          v-for="option in interrupt.outbound_options"
          :key="option.option_id"
          :class="['rail-option', { selected: outboundId === option.option_id }]"
        >
          <label>
            <input
              :checked="outboundId === option.option_id"
              type="radio"
              name="outbound"
              :disabled="busy"
              @change="chooseRail('outbound', option)"
            />
            <span class="train-code">{{ option.train_code }}</span>
            <span class="rail-times"><strong>{{ option.departure_time }}</strong><ArrowRight :size="14" /><strong>{{ option.arrival_time }}</strong></span>
            <span class="rail-stations">{{ option.from_station }} → {{ option.to_station }}</span>
            <span class="rail-meta"><Clock3 :size="13" />{{ duration(option.duration_minutes) }}</span>
            <span class="rail-price">{{ money(option.price_from) }}</span>
          </label>
          <select
            v-if="outboundId === option.option_id && option.seats?.length"
            v-model="seatTypes[option.option_id]"
            :disabled="busy"
            :aria-label="`${option.train_code} 去程席别`"
          >
            <option v-for="seat in option.seats" :key="seat.name" :value="seat.name">
              {{ seat.name }} · {{ seat.availability || "余票未知" }} · {{ money(seat.price) }}
            </option>
          </select>
        </article>
        <label v-if="allowSelfArranged" class="self-arranged-option">
          <input v-model="outboundId" type="radio" name="outbound" :value="selfValue" :disabled="busy" />
          <span><strong>去程自行安排</strong><small>不使用当前铁路候选</small></span>
        </label>
      </div>
    </section>

    <section class="selection-group" aria-labelledby="return-title">
      <div class="selection-group-heading">
        <TrainFront :size="18" />
        <div><h4 id="return-title">返程车次</h4><p>返程与去程分别确认</p></div>
      </div>
      <div class="rail-option-list">
        <article
          v-for="option in interrupt.return_options"
          :key="option.option_id"
          :class="['rail-option', { selected: returnId === option.option_id }]"
        >
          <label>
            <input
              :checked="returnId === option.option_id"
              type="radio"
              name="return"
              :disabled="busy"
              @change="chooseRail('return', option)"
            />
            <span class="train-code">{{ option.train_code }}</span>
            <span class="rail-times"><strong>{{ option.departure_time }}</strong><ArrowRight :size="14" /><strong>{{ option.arrival_time }}</strong></span>
            <span class="rail-stations">{{ option.from_station }} → {{ option.to_station }}</span>
            <span class="rail-meta"><Clock3 :size="13" />{{ duration(option.duration_minutes) }}</span>
            <span class="rail-price">{{ money(option.price_from) }}</span>
          </label>
          <select
            v-if="returnId === option.option_id && option.seats?.length"
            v-model="seatTypes[option.option_id]"
            :disabled="busy"
            :aria-label="`${option.train_code} 返程席别`"
          >
            <option v-for="seat in option.seats" :key="seat.name" :value="seat.name">
              {{ seat.name }} · {{ seat.availability || "余票未知" }} · {{ money(seat.price) }}
            </option>
          </select>
        </article>
        <label v-if="allowSelfArranged" class="self-arranged-option">
          <input v-model="returnId" type="radio" name="return" :value="selfValue" :disabled="busy" />
          <span><strong>返程自行安排</strong><small>不使用当前铁路候选</small></span>
        </label>
      </div>
    </section>

    <section v-if="requiresHotel" class="selection-group" aria-labelledby="hotel-title">
      <div class="selection-group-heading">
        <BedDouble :size="18" />
        <div><h4 id="hotel-title">住宿</h4><p>RollingGo 实时结果优先，本地目录作为降级</p></div>
      </div>
      <div class="hotel-option-list">
        <label
          v-for="item in interrupt.hotel_options"
          :key="item.hotel_id"
          :class="['hotel-option', { selected: hotelId === item.hotel_id }]"
        >
          <input v-model="hotelId" type="radio" name="hotel" :value="item.hotel_id" :disabled="busy" />
          <span class="hotel-image">
            <img
              v-if="item.image_url && !failedImages.has(item.hotel_id)"
              :src="item.image_url"
              :alt="`${item.name}图片`"
              width="112"
              height="82"
              loading="lazy"
              @error="hideImage(item.hotel_id)"
            />
            <span v-else class="image-placeholder"><ImageOff :size="22" /><small>暂无图片</small></span>
          </span>
          <span class="hotel-copy">
            <strong>{{ item.name }}</strong>
            <small><MapPin :size="13" />{{ locationText(item) }}</small>
            <a
              v-if="mapUrl(item)"
              class="map-link"
              :href="mapUrl(item) || undefined"
              target="_blank"
              rel="noreferrer"
              @click.stop
            >
              <ExternalLink :size="12" />查看地图
            </a>
            <span>
              <small v-if="item.star_rating">{{ item.star_rating }} 星</small>
              <small v-if="item.distance_km !== null && item.distance_km !== undefined">距中心 {{ item.distance_km }} km</small>
              <small>{{ item.source || "unknown" }}</small>
            </span>
          </span>
          <span class="hotel-price"><strong>{{ money(item.price_per_night) }}</strong><small>每晚参考</small></span>
          <Check v-if="hotelId === item.hotel_id" class="option-check" :size="18" />
        </label>
        <label v-if="allowSelfArranged" class="self-arranged-option hotel-self-option">
          <input v-model="hotelId" type="radio" name="hotel" :value="selfValue" :disabled="busy" />
          <Hotel :size="18" />
          <span><strong>住宿自行安排</strong><small>酒店价格不会计入已知总额</small></span>
        </label>
      </div>
    </section>

    <section v-else class="one-day-notice">
      <BedDouble :size="18" />
      <div><strong>本次无需选择住宿</strong><p>一日游自动跳过酒店查询与住宿预算。</p></div>
    </section>

    <footer class="interrupt-footer">
      <p>{{ complete ? "选择已完整，可以继续生成。" : "请完成所有必选项，或选择自行安排。" }}</p>
      <button class="primary-button" type="button" :disabled="busy || !complete" @click="submit">
        <LoaderCircle v-if="busy" class="spin" :size="17" />
        <ArrowRight v-else :size="17" />
        生成行程
      </button>
    </footer>
  </section>
</template>
