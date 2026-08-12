<!-- 只读地图组件：展示后端确认过的 POI，不参与路线或地点推断。 -->
<script setup lang="ts">
import { onMounted, ref } from "vue";

import type { DayPlan } from "./types";

const props = defineProps<{ day: DayPlan }>();

const mapElement = ref<HTMLDivElement | null>(null);
const amapKey = import.meta.env.VITE_AMAP_JS_KEY as string | undefined;
const mapMessage = ref("");

onMounted(() => {
  if (!amapKey) {
    mapMessage.value = "配置 VITE_AMAP_JS_KEY 后显示地图。当前仍可查看地点和路线数据。";
    return;
  }
  loadMapScript().then(renderMap).catch(() => {
    mapMessage.value = "高德地图加载失败，请检查前端 Key 和域名白名单。";
  });
});

function loadMapScript(): Promise<void> {
  if (window.AMap) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${amapKey}`;
    script.onload = () => resolve();
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

function renderMap(): void {
  if (!mapElement.value || !window.AMap) return;
  const AMap = window.AMap;
  const spots = props.day.activities;
  const center = spots[0] ? [spots[0].longitude, spots[0].latitude] : [116.397, 39.909];
  const map = new AMap.Map(mapElement.value, { zoom: 12, center });
  spots.forEach((spot, index) => {
    const marker = new AMap.Marker({
      position: [spot.longitude, spot.latitude],
      title: `${index + 1}. ${spot.name}`,
    });
    map.add(marker);
  });
  const path = spots.map((spot) => [spot.longitude, spot.latitude]);
  if (path.length > 1) map.add(new AMap.Polyline({ path, strokeColor: "#1f7a68", strokeWeight: 4 }));
}
</script>

<template>
  <div ref="mapElement" class="trip-map">
    <span v-if="mapMessage">{{ mapMessage }}</span>
  </div>
</template>

<script lang="ts">
declare global {
  interface Window {
    AMap?: {
      Map: new (element: HTMLElement, options: Record<string, unknown>) => { add: (item: unknown) => void };
      Marker: new (options: Record<string, unknown>) => unknown;
      Polyline: new (options: Record<string, unknown>) => unknown;
    };
  }
}
</script>
