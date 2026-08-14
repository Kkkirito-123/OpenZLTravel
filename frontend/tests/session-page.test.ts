// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SessionPage from "../src/pages/SessionPage.vue";
import type { PlanningSession } from "../src/types";

const api = vi.hoisted(() => ({
  generatePlanningSession: vi.fn(),
  getHotelDetail: vi.fn(),
  getPlanningSession: vi.fn(),
  getReadiness: vi.fn(),
  loadRailTransfers: vi.fn(),
  retryPlanningSession: vi.fn(),
  updatePlanningSelection: vi.fn(),
}));
const replace = vi.hoisted(() => vi.fn());

vi.mock("../src/api", () => ({
  ...api,
  errorMessage: () => "请求失败，请重试",
}));
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { sessionId: "session-1" } }),
  useRouter: () => ({ replace }),
}));

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset());
  api.getReadiness.mockResolvedValue({ hotel_provider: "login_required" });
  replace.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("SessionPage", () => {
  it("轮询恢复查询状态并独立展示降级步骤", async () => {
    vi.useFakeTimers();
    api.getPlanningSession
      .mockResolvedValueOnce(sampleSession("searching"))
      .mockResolvedValueOnce(sampleSession("awaiting_selection", true));
    const wrapper = mount(SessionPage);
    await flushPromises();

    expect(wrapper.text()).toContain("查询中");
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();

    expect(api.getPlanningSession).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("12306 暂时不可用，可选择自行安排");
    expect(wrapper.find(".step-status.degraded").exists()).toBe(true);
    wrapper.unmount();
  });

  it("酒店详情只在点击后加载，并在图片失败后显示占位", async () => {
    api.getPlanningSession.mockResolvedValue(sampleSession("awaiting_selection"));
    api.getHotelDetail.mockResolvedValue({
      hotel_id: "h1",
      name: "湖畔酒店",
      address: "湖畔路 8 号",
      description: "安静住宿",
      facilities: ["Wi-Fi"],
      images: [],
      rooms: [{
        room_id: "r1", name: "标准房", price: 420, breakfast: "含早",
        cancellation: "入住前可取消", available: true,
      }],
      booking_url: null,
      source: "rollinggo",
    });
    const wrapper = mount(SessionPage);
    await flushPromises();

    expect(api.getHotelDetail).not.toHaveBeenCalled();
    const image = wrapper.get(".hotel-thumb img");
    await image.trigger("error");
    expect(wrapper.find(".hotel-thumb img").exists()).toBe(false);
    expect(wrapper.text()).toContain("暂无酒店图片");

    await wrapper.get('button[aria-label="查看酒店详情"]').trigger("click");
    await flushPromises();
    expect(api.getHotelDetail).toHaveBeenCalledWith("session-1", "h1");
    expect(wrapper.text()).toContain("RollingGo 实时详情");
    expect(wrapper.text()).toContain("标准房");
    expect(wrapper.text()).toContain("入住前可取消");
  });

  it("一日游只确认往返交通即可生成", async () => {
    const session = sampleSession("awaiting_selection");
    session.request.end_date = session.request.start_date;
    session.selection = {
      outbound: null,
      return_trip: null,
      hotel_id: null,
      self_arranged_outbound: true,
      self_arranged_return: true,
      self_arranged_hotel: false,
    };
    api.getPlanningSession.mockResolvedValue(session);
    api.generatePlanningSession.mockResolvedValue({ ...session, status: "generating" });
    const wrapper = mount(SessionPage);
    await flushPromises();

    const generateButton = wrapper.get(".selection-footer .primary-button");
    expect(wrapper.text()).toContain("一日行程无需选择住宿");
    expect(wrapper.find(".hotel-section").exists()).toBe(false);
    expect(generateButton.attributes("disabled")).toBeUndefined();
    await generateButton.trigger("click");
    await flushPromises();

    expect(api.generatePlanningSession).toHaveBeenCalledWith("session-1");
  });

  it("选择车次会提交真实 option_id 与席别", async () => {
    const session = sampleSession("awaiting_selection");
    api.getPlanningSession.mockResolvedValue(session);
    api.updatePlanningSelection.mockImplementation(async (_id, selection) => ({
      ...session,
      selection,
    }));
    const wrapper = mount(SessionPage);
    await flushPromises();

    await wrapper.get(".rail-table .select-button").trigger("click");
    await flushPromises();

    expect(api.updatePlanningSelection).toHaveBeenCalledWith(
      "session-1",
      expect.objectContaining({
        outbound: { option_id: "outbound-g1", seat_type: "二等座" },
        self_arranged_outbound: false,
      }),
    );
    expect(wrapper.text()).toContain("去程车次已选择");
    expect(wrapper.get(".rail-table .select-button").text()).toContain("已选择");
  });

  it("未登录 RollingGo 时明确显示本地住宿降级状态", async () => {
    api.getPlanningSession.mockResolvedValue(sampleSession("awaiting_selection"));
    const wrapper = mount(SessionPage);
    await flushPromises();

    expect(wrapper.text()).toContain("RollingGo 未登录，当前使用本地候选");
  });
});

function sampleSession(
  status: PlanningSession["status"],
  degraded = false,
): PlanningSession {
  const rail = (direction: "outbound" | "return") => ({
    option_id: `${direction}-g1`,
    direction,
    travel_date: direction === "outbound" ? "2026-09-01" : "2026-09-03",
    train_code: "G1",
    train_type: "高铁",
    from_station: direction === "outbound" ? "北京南" : "杭州东",
    to_station: direction === "outbound" ? "杭州东" : "北京南",
    departure_time: "08:00",
    arrival_time: "12:00",
    duration_minutes: 240,
    seats: [{ name: "二等座", availability: "有", price: 200 }],
    price_from: 200,
    has_ticket: true,
    is_transfer: false,
    transfer_station: null,
    segments: [],
    booking_url: "https://www.12306.cn/",
  });
  return {
    session_id: "session-1",
    status,
    request: {
      origin: "北京",
      destination: "杭州",
      start_date: "2026-09-01",
      end_date: "2026-09-03",
      travelers: 2,
      budget: 5000,
      pace: "适中",
      hotel_level: "舒适",
      transport_mode: "auto",
      preferences: [],
      dietary_preferences: [],
      notes: "",
    },
    steps: [
      {
        name: "poi", label: "地点数据", status: status === "searching" ? "running" : "completed",
        attempts: 1, duration_ms: status === "searching" ? null : 20,
        cache_hit: false, message: null, error_code: null,
      },
      {
        name: "rail_outbound", label: "去程车票", status: degraded ? "degraded" : "completed",
        attempts: 1, duration_ms: 40, cache_hit: false,
        message: degraded ? "12306 暂时不可用，可选择自行安排" : null,
        error_code: degraded ? "rail_unavailable" : null,
      },
    ],
    outbound_options: [rail("outbound")],
    return_options: [rail("return")],
    outbound_transfers: [],
    return_transfers: [],
    hotel_options: [{
      hotel_id: "h1", name: "湖畔酒店", address: "湖畔路 8 号",
      latitude: 30.1, longitude: 120.1, star_rating: 4,
      price_per_night: 420, total_price: 840, distance_km: 1.2,
      image_url: "https://images.example.com/hotel.jpg", facilities: ["Wi-Fi"],
      source: "dida", booking_url: null,
    }],
    weather: [{
      date: "2026-09-01", day_weather: "晴", night_weather: "晴",
      day_temperature: "26", night_temperature: "18", warning: null,
      source: { provider: "open_meteo", freshness: "forecast" },
    }],
    candidates: null,
    selection: {
      outbound: null, return_trip: null, hotel_id: null,
      self_arranged_outbound: false, self_arranged_return: false,
      self_arranged_hotel: false,
    },
    trip_id: null,
    warnings: [],
    error_code: null,
    error_message: null,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
  };
}
