<!-- 只读地图组件：只渲染后端返回的真实 POI 和高德路线轨迹。 -->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import type { Coordinate, DayPlan } from "./types";

const props = defineProps<{ day: DayPlan }>();
const mapElement = ref<HTMLDivElement | null>(null);
const amapKey = import.meta.env.VITE_AMAP_JS_KEY as string | undefined;
const mapMessage = ref("");
const hasMissingTrack = computed(() => props.day.routes.some((route) => !route.polyline.length));

let mapScriptPromise: Promise<void> | null = null;

onMounted(async () => {
  if (!amapKey) {
    mapMessage.value = "配置 VITE_AMAP_JS_KEY 后显示地图。当前仍可查看地点和路线数据。";
    return;
  }
  try {
    await loadMapScript(amapKey);
    renderMap();
  } catch {
    mapMessage.value = "高德地图加载失败，请检查前端 Key 和域名白名单。";
  }
});

function loadMapScript(key: string): Promise<void> {
  if (window.AMap) return Promise.resolve();
  if (mapScriptPromise) return mapScriptPromise;
  mapScriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${key}`;
    script.dataset.openzltravelAmap = "true";
    script.onload = () => resolve();
    script.onerror = () => {
      mapScriptPromise = null;
      reject(new Error("高德地图脚本加载失败"));
    };
    document.head.appendChild(script);
  });
  return mapScriptPromise;
}

function renderMap(): void {
  if (!mapElement.value || !window.AMap) return;
  const spots = props.day.activities;
  const center = spots[0] ? position(spots[0]) : [116.397, 39.909];
  const map = new window.AMap.Map(mapElement.value, { zoom: 12, center });
  const overlays: unknown[] = spots.map(
    (spot, index) =>
      new window.AMap!.Marker({
        position: position(spot),
        title: `${index + 1}. ${spot.name}`,
      }),
  );
  overlays.push(
    ...props.day.routes
      .filter((route) => route.polyline.length > 0)
      .map(
        (route) =>
          new window.AMap!.Polyline({
            path: route.polyline.map(position),
            strokeColor: "#1f7a68",
            strokeWeight: 5,
            strokeOpacity: 0.85,
          }),
      ),
  );
  map.add(overlays);
  if (overlays.length > 1) map.setFitView(overlays, false, [28, 28, 28, 28]);
  if (hasMissingTrack.value) mapMessage.value = "部分路线暂无轨迹，仅显示对应地点。";
}

function position(point: Coordinate): [number, number] {
  return [point.longitude, point.latitude];
}
</script>

<template>
  <div class="trip-map-shell">
    <div ref="mapElement" class="trip-map" />
    <p v-if="mapMessage" class="map-message">{{ mapMessage }}</p>
  </div>
</template>

<script lang="ts">
interface AMapInstance {
  add: (items: unknown | unknown[]) => void;
  setFitView: (items: unknown[], immediately?: boolean, padding?: number[]) => void;
}

declare global {
  interface Window {
    AMap?: {
      Map: new (element: HTMLElement, options: Record<string, unknown>) => AMapInstance;
      Marker: new (options: Record<string, unknown>) => unknown;
      Polyline: new (options: Record<string, unknown>) => unknown;
    };
  }
}
</script>
