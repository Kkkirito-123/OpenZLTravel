import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ItineraryPanel from "../src/components/ItineraryPanel.vue";
import type { TripRecord } from "../src/types";

describe("ItineraryPanel", () => {
  it("使用 Provider place_index 展示地点名称与地址，缺失时才回退事实 ID", () => {
    const trip: TripRecord = {
      trip_id: "trip-1",
      requirements: { destination: "杭州", start_date: "2026-10-01", end_date: "2026-10-01" },
      city: { name: "杭州" },
      selection: {
        attraction_ids: ["poi-west-lake", "poi-missing"],
        outbound: { option_id: "out-1", seat_type: "二等座" },
        return_trip: { option_id: "return-1", seat_type: "二等座" },
      },
      outbound_rail: {
        option_id: "out-1",
        direction: "outbound",
        travel_date: "2026-10-01",
        train_code: "G1",
        from_station: "上海虹桥",
        to_station: "杭州东",
        departure_time: "08:00",
        arrival_time: "08:45",
        seats: [{ name: "二等座", availability: "有票", price: 73 }],
        price_from: 73,
        booking_url: "https://www.12306.cn/index/",
      },
      return_rail: {
        option_id: "return-1",
        direction: "return",
        travel_date: "2026-10-01",
        train_code: "G2",
        from_station: "杭州东",
        to_station: "上海虹桥",
        departure_time: "19:00",
        arrival_time: "19:45",
        price_from: 73,
      },
      draft: {
        summary: "杭州一日游",
        days: [{
          day_index: 1,
          theme: "西湖人文",
          activities: [
            { poi_id: "poi-west-lake", start_time: "09:00", duration_minutes: 180 },
            { poi_id: "poi-missing", start_time: "14:00", duration_minutes: 60 },
          ],
          meal_ids: ["poi-restaurant"],
          hotel_id: "poi-hotel",
        }],
      },
      place_index: {
        "poi-west-lake": {
          fact_id: "poi-west-lake",
          name: "西湖风景名胜区",
          address: "杭州市西湖区龙井路1号",
          category: "attraction",
          image_url: "https://images.example.com/west-lake.jpg",
        },
        "poi-restaurant": {
          fact_id: "poi-restaurant",
          name: "楼外楼",
          address: "孤山路30号",
          category: "restaurant",
        },
        "poi-hotel": {
          fact_id: "poi-hotel",
          name: "湖畔酒店",
          address: "北山街1号",
          category: "hotel",
        },
      },
      weather: [{
        date: "2026-10-01",
        day_weather: "晴",
        day_temperature: "25",
      }],
      routes: {},
      budget: { total_known: 146, currency: "CNY" },
    };

    const wrapper = mount(ItineraryPanel, { props: { trip } });

    expect(wrapper.text()).toContain("西湖风景名胜区");
    expect(wrapper.text()).toContain("杭州市西湖区龙井路1号");
    expect(wrapper.text()).toContain("楼外楼");
    expect(wrapper.text()).toContain("湖畔酒店");
    expect(wrapper.text()).toContain("poi-missing");
    expect(wrapper.text()).toContain("晴 · 25℃");
    expect(wrapper.text()).toContain("城际交通");
    expect(wrapper.text()).toContain("上海虹桥 → 杭州东");
    expect(wrapper.text()).toContain("二等座");
    expect(wrapper.text()).toContain("¥73");
    expect(wrapper.get('img[alt="西湖风景名胜区推荐图片"]').attributes("loading")).toBe("lazy");
    expect(wrapper.text()).toContain("图片来源：images.example.com");
  });

  it("图片加载失败后隐藏图片并显示明确降级", async () => {
    const failedTrip: TripRecord = {
      trip_id: "trip-image-failed",
      requirements: { destination: "西安" },
      city: { name: "西安" },
      selection: { attraction_ids: ["terracotta"] },
      draft: {
        summary: "西安行程",
        days: [{
          day_index: 1,
          theme: "人文",
          activities: [{ poi_id: "terracotta", start_time: "09:00", duration_minutes: 180 }],
        }],
      },
      place_index: {
        terracotta: {
          fact_id: "terracotta",
          name: "秦始皇帝陵博物院",
          address: "临潼区秦陵北路",
          category: "attraction",
          image_url: "https://images.example.com/terracotta.jpg",
        },
      },
      weather: [],
      routes: {},
      budget: { total_known: 0, currency: "CNY" },
    };
    const wrapper = mount(ItineraryPanel, { props: { trip: failedTrip } });

    await wrapper.get(".recommendation-image").trigger("error");

    expect(wrapper.find(".recommendation-image").exists()).toBe(false);
    expect(wrapper.text()).toContain("推荐图片加载失败");

    await wrapper.setProps({
      trip: {
        ...failedTrip,
        place_index: {
          terracotta: {
            ...failedTrip.place_index?.terracotta,
            fact_id: "terracotta",
            name: "秦始皇帝陵博物院",
            address: "临潼区秦陵北路",
            category: "attraction",
            image_url: "https://cdn.example.com/terracotta-new.jpg",
          },
        },
      },
    });

    expect(wrapper.get(".recommendation-image").attributes("src"))
      .toBe("https://cdn.example.com/terracotta-new.jpg");
  });
});
