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
    };

    const wrapper = mount(ItineraryPanel, { props: { trip } });

    expect(wrapper.text()).toContain("西湖风景名胜区");
    expect(wrapper.text()).toContain("杭州市西湖区龙井路1号");
    expect(wrapper.text()).toContain("楼外楼");
    expect(wrapper.text()).toContain("湖畔酒店");
    expect(wrapper.text()).toContain("poi-missing");
  });
});
