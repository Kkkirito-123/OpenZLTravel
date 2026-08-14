<!-- 只读地图组件：只渲染后端返回的真实 POI 和高德路线轨迹。 -->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import type { Coordinate, DayPlan } from "./types";

const props = defineProps<{ day: DayPlan }>();
const emit = defineEmits<{ "select-place": [poiId: string] }>();
const mapElement = ref<HTMLDivElement | null>(null);
const amapKey = import.meta.env.VITE_AMAP_JS_KEY as string | undefined;
const mapMessage = ref("");
const hasMissingTrack = computed(() =>
  props.day.routes.some((route) => !route.polyline.length && route.source?.provider !== "local_estimate"),
);
const hasEstimatedRoute = computed(() =>
  props.day.routes.some((route) => route.source?.provider === "local_estimate"),
);

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
  const overlays: AMapOverlay[] = spots.map((spot, index) => {
    const marker = new window.AMap!.Marker({
        position: position(spot),
        title: `${index + 1}. ${spot.name}`,
      });
    // 地图只负责发出 POI ID，详情内容由结果页统一展示，避免地图组件重复维护业务状态。
    marker.on?.("click", () => emit("select-place", spot.poi_id));
    return marker;
  });
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
  if (hasEstimatedRoute.value) mapMessage.value = "当前路线为本地估算，未绘制虚假道路轨迹。";
}

function position(point: Coordinate): [number, number] {
  return wgs84ToGcj02(point.latitude, point.longitude);
}

function wgs84ToGcj02(latitude: number, longitude: number): [number, number] {
  if (longitude < 73 || longitude > 135 || latitude < 3 || latitude > 54) {
    return [longitude, latitude];
  }
  const dLat = transformLat(longitude - 105, latitude - 35);
  const dLon = transformLon(longitude - 105, latitude - 35);
  const radLat = latitude / 180 * Math.PI;
  const magic = Math.sin(radLat);
  const adjusted = 1 - 0.00669342162296594323 * magic * magic;
  const sqrtAdjusted = Math.sqrt(adjusted);
  const deltaLat = dLat * 180 / (6335552.717000426 * adjusted * sqrtAdjusted * Math.PI);
  const deltaLon = dLon * 180 / (6378245 / sqrtAdjusted * Math.cos(radLat) * Math.PI);
  return [longitude + deltaLon, latitude + deltaLat];
}

function transformLat(x: number, y: number): number {
  let value = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  value += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
  value += (20 * Math.sin(y * Math.PI) + 40 * Math.sin(y / 3 * Math.PI)) * 2 / 3;
  value += (160 * Math.sin(y / 12 * Math.PI) + 320 * Math.sin(y * Math.PI / 30)) * 2 / 3;
  return value;
}

function transformLon(x: number, y: number): number {
  let value = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  value += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
  value += (20 * Math.sin(x * Math.PI) + 40 * Math.sin(x / 3 * Math.PI)) * 2 / 3;
  value += (150 * Math.sin(x / 12 * Math.PI) + 300 * Math.sin(x / 30 * Math.PI)) * 2 / 3;
  return value;
}
</script>

<template>
  <div class="trip-map-shell">
    <div ref="mapElement" class="trip-map" />
    <p v-if="mapMessage" class="map-message">{{ mapMessage }}</p>
  </div>
</template>

<script lang="ts">
interface AMapOverlay {
  on?: (event: string, handler: () => void) => void;
}

interface AMapInstance {
  add: (items: AMapOverlay | AMapOverlay[]) => void;
  setFitView: (items: AMapOverlay[], immediately?: boolean, padding?: number[]) => void;
}

declare global {
  interface Window {
    AMap?: {
      Map: new (element: HTMLElement, options: Record<string, unknown>) => AMapInstance;
      Marker: new (options: Record<string, unknown>) => AMapOverlay;
      Polyline: new (options: Record<string, unknown>) => AMapOverlay;
    };
  }
}
</script>
