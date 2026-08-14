// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import TripPage from "../src/pages/TripPage.vue";
import type { Itinerary } from "../src/types";

const api = vi.hoisted(() => ({
  editTripDay: vi.fn(),
  errorMessage: vi.fn(() => "请求失败"),
  getTrip: vi.fn(),
  getTripAlternatives: vi.fn(),
}));
vi.mock("../src/api", () => ({
  ...api,
  markdownUrl: (tripId: string) => `/export/${tripId}`,
}));
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { tripId: "trip-1" } }),
  useRouter: () => ({ push: vi.fn() }),
}));

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset());
  api.errorMessage.mockReturnValue("请求失败");
});

describe("TripPage", () => {
  it("展示每日预算并在图片加载失败后降级为占位区域", async () => {
    api.getTrip.mockResolvedValue(sampleItinerary(true));
    const wrapper = mount(TripPage, { global: { stubs: { TripMap: true } } });
    await flushPromises();

    expect(wrapper.text()).toContain("当日估算 ¥600");
    const image = wrapper.get("img.spot-image");
    await image.trigger("error");
    expect(wrapper.find("img.spot-image").exists()).toBe(false);
    expect(wrapper.text()).toContain("暂无图片");
  });

  it("旧行程没有每日预算时显示兼容提示", async () => {
    api.getTrip.mockResolvedValue(sampleItinerary(false));
    const wrapper = mount(TripPage, { global: { stubs: { TripMap: true } } });
    await flushPromises();

    expect(wrapper.text()).toContain("旧行程暂无每日预算明细");
  });

  it("点击景点后打开地点详情抽屉", async () => {
    api.getTrip.mockResolvedValue(sampleItinerary(true));
    const wrapper = mount(TripPage, { global: { stubs: { TripMap: true } } });
    await flushPromises();

    await wrapper.get('button[aria-label="查看湖畔公园详情"]').trigger("click");

    expect(wrapper.text()).toContain("景点");
    expect(wrapper.text()).toContain("湖畔公园");
    expect(wrapper.text()).toContain("湖畔路");
    expect(wrapper.text()).toContain("已生成行程中的真实 POI 数据");
  });

  it("编辑当天后提交版本号并展示重算结果", async () => {
    const original = sampleItinerary(true);
    api.getTrip.mockResolvedValue(original);
    api.getTripAlternatives.mockResolvedValue({
      trip_id: "trip-1",
      revision: 1,
      attractions: [samplePoi()],
    });
    api.editTripDay.mockResolvedValue({ ...original, revision: 2 });
    const wrapper = mount(TripPage, { global: { stubs: { TripMap: true } } });
    await flushPromises();

    await findButton(wrapper, "编辑当天").trigger("click");
    await flushPromises();
    await wrapper.get('.edit-fields input[type="time"]').setValue("10:00");
    await findButton(wrapper, "保存并重算").trigger("click");
    await flushPromises();

    expect(api.editTripDay).toHaveBeenCalledWith(
      "trip-1",
      1,
      {
        expected_revision: 1,
        activities: [{ poi_id: "a1", start_time: "10:00", duration_minutes: 90 }],
      },
    );
    expect(wrapper.text()).toContain("版本 2");
    expect(wrapper.find(".edit-drawer").exists()).toBe(false);
  });

  it("版本冲突时保留编辑内容并展示恢复提示", async () => {
    api.getTrip.mockResolvedValue(sampleItinerary(true));
    api.getTripAlternatives.mockResolvedValue({
      trip_id: "trip-1",
      revision: 1,
      attractions: [samplePoi()],
    });
    api.editTripDay.mockRejectedValue(new Error("conflict"));
    api.errorMessage.mockReturnValue("行程已经更新，请刷新后重试");
    const wrapper = mount(TripPage, { global: { stubs: { TripMap: true } } });
    await flushPromises();

    await findButton(wrapper, "编辑当天").trigger("click");
    await flushPromises();
    await wrapper.get('.edit-fields input[type="time"]').setValue("10:00");
    await findButton(wrapper, "保存并重算").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("行程已经更新，请刷新后重试");
    expect(wrapper.find(".edit-drawer").exists()).toBe(true);
    expect(wrapper.get('.edit-fields input[type="time"]').element).toHaveProperty(
      "value",
      "10:00",
    );
  });
});

function findButton(wrapper: ReturnType<typeof mount>, text: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(text));
  if (!button) throw new Error(`找不到按钮：${text}`);
  return button;
}

function samplePoi() {
  return {
    id: "a1",
    name: "湖畔公园",
    address: "湖畔路",
    category: "attraction" as const,
    latitude: 30.1,
    longitude: 120.1,
    type_name: "公园",
    image_url: "https://images.example.com/park.jpg",
  };
}

function sampleItinerary(withBudget: boolean): Itinerary {
  return {
    trip_id: "trip-1",
    planning_session_id: "session-1",
    revision: 1,
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
