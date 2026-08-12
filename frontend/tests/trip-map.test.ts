// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TripMap from "../src/TripMap.vue";
import type { DayPlan } from "../src/types";

const polyline = vi.fn(function (options: unknown) {
  return { options };
});
const setFitView = vi.fn();

beforeEach(() => {
  vi.stubEnv("VITE_AMAP_JS_KEY", "test-key");
  polyline.mockClear();
  setFitView.mockClear();
  window.AMap = {
    Map: vi.fn(function () {
      return { add: vi.fn(), setFitView };
    }) as unknown as NonNullable<Window["AMap"]>["Map"],
    Marker: vi.fn(function (options: unknown) {
      return { options };
    }),
    Polyline: polyline,
  } as unknown as NonNullable<Window["AMap"]>;
});

afterEach(() => vi.unstubAllEnvs());

describe("TripMap", () => {
  it("只使用后端返回的真实路线轨迹", async () => {
    mount(TripMap, { props: { day: sampleDay() } });
    await flushPromises();

    expect(polyline).toHaveBeenCalledOnce();
    expect(polyline.mock.calls[0][0]).toMatchObject({
      path: [
        [120.1, 30.1],
        [120.15, 30.15],
        [120.2, 30.2],
      ],
    });
    expect(setFitView).toHaveBeenCalledOnce();
  });

  it("轨迹缺失时不绘制直线并显示提示", async () => {
    const day = sampleDay();
    day.routes[0].polyline = [];

    const wrapper = mount(TripMap, { props: { day } });
    await flushPromises();

    expect(polyline).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("部分路线暂无轨迹");
  });
});

function sampleDay(): DayPlan {
  return {
    day_index: 1,
    date: "2026-09-01",
    theme: "湖畔漫游",
    activities: [
      {
        poi_id: "a1",
        name: "起点",
        address: "起点路",
        latitude: 30.1,
        longitude: 120.1,
        start_time: "09:00",
        duration_minutes: 90,
        note: "",
        image_url: null,
      },
      {
        poi_id: "a2",
        name: "终点",
        address: "终点路",
        latitude: 30.2,
        longitude: 120.2,
        start_time: "11:00",
        duration_minutes: 90,
        note: "",
        image_url: null,
      },
    ],
    meals: [],
    hotel: null,
    routes: [
      {
        from_poi_id: "a1",
        to_poi_id: "a2",
        distance_km: 2.5,
        duration_minutes: 15,
        mode: "驾车",
        polyline: [
          { latitude: 30.1, longitude: 120.1 },
          { latitude: 30.15, longitude: 120.15 },
          { latitude: 30.2, longitude: 120.2 },
        ],
      },
    ],
    weather: {
      date: "2026-09-01",
      day_weather: "晴",
      night_weather: "晴",
      day_temperature: "25",
      night_temperature: "18",
      warning: null,
    },
    budget: null,
    notes: [],
  };
}
