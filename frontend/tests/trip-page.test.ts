// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import TripPage from "../src/pages/TripPage.vue";
import type { Itinerary } from "../src/types";

const { getTrip } = vi.hoisted(() => ({ getTrip: vi.fn() }));
vi.mock("../src/api", () => ({
  errorMessage: () => "请求失败",
  getTrip,
  markdownUrl: (tripId: string) => `/export/${tripId}`,
}));
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { tripId: "trip-1" } }),
  useRouter: () => ({ push: vi.fn() }),
}));

beforeEach(() => getTrip.mockReset());

describe("TripPage", () => {
  it("展示每日预算并在图片加载失败后降级为占位区域", async () => {
    getTrip.mockResolvedValue(sampleItinerary(true));
    const wrapper = mount(TripPage, { global: { stubs: { TripMap: true } } });
    await flushPromises();

    expect(wrapper.text()).toContain("当日估算 ¥600");
    const image = wrapper.get("img.spot-image");
    await image.trigger("error");
    expect(wrapper.find("img.spot-image").exists()).toBe(false);
    expect(wrapper.text()).toContain("暂无图片");
  });

  it("旧行程没有每日预算时显示兼容提示", async () => {
    getTrip.mockResolvedValue(sampleItinerary(false));
    const wrapper = mount(TripPage, { global: { stubs: { TripMap: true } } });
    await flushPromises();

    expect(wrapper.text()).toContain("旧行程暂无每日预算明细");
  });
});

function sampleItinerary(withBudget: boolean): Itinerary {
  return {
    trip_id: "trip-1",
    destination: "测试市",
    start_date: "2026-09-01",
    end_date: "2026-09-01",
    travelers: 2,
    summary: "测试行程",
    created_at: "2026-08-13T00:00:00Z",
    budget: { transport: 40, hotel: 0, meals: 360, tickets: 160, other: 40, total: 600 },
    tips: [],
    warnings: [],
    days: [
      {
        day_index: 1,
        date: "2026-09-01",
        theme: "湖畔漫游",
        activities: [
          {
            poi_id: "a1",
            name: "湖畔公园",
            address: "湖畔路",
            latitude: 30.1,
            longitude: 120.1,
            start_time: "09:00",
            duration_minutes: 90,
            note: "轻松游览",
            image_url: "https://images.example.com/park.jpg",
          },
        ],
        meals: [],
        hotel: null,
        routes: [],
        weather: {
          date: "2026-09-01",
          day_weather: "晴",
          night_weather: "晴",
          day_temperature: "25",
          night_temperature: "18",
          warning: null,
        },
        budget: withBudget
          ? { transport: 40, hotel: 0, meals: 360, tickets: 160, other: 40, total: 600 }
          : null,
        notes: [],
      },
    ],
  };
}
