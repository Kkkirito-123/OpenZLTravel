import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import InterruptPanel from "../src/components/InterruptPanel.vue";
import type { TravelInterrupt } from "../src/types";

describe("InterruptPanel", () => {
  it("提交 clarification 恢复载荷", async () => {
    const interrupt: TravelInterrupt = {
      kind: "clarification",
      question: "请补充出发日期",
      missing_fields: ["start_date"],
    };
    const wrapper = mount(InterruptPanel, {
      props: { interrupt, selection: {}, busy: false },
    });

    await wrapper.get('input[type="date"]').setValue("2026-10-01");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("resume")?.[0]).toEqual([
      { kind: "clarification", values: { start_date: "2026-10-01" } },
    ]);
  });

  it("提交 destination_selection 恢复载荷", async () => {
    const interrupt: TravelInterrupt = {
      kind: "destination_selection",
      candidates: [{
        candidate_id: "hangzhou",
        city: { name: "杭州" },
        score: 0.92,
        reasons: ["人文景点覆盖高"],
        attraction_count: 18,
        restaurant_count: 20,
        hotel_count: 16,
      }],
    };
    const wrapper = mount(InterruptPanel, {
      props: { interrupt, selection: {}, busy: false },
    });

    await wrapper.get('input[value="hangzhou"]').setValue();
    await wrapper.get(".interrupt-footer button").trigger("click");

    expect(wrapper.emitted("resume")?.[0]).toEqual([
      { kind: "destination_selection", candidate_id: "hangzhou" },
    ]);
  });

  it("提交 travel_selection 恢复载荷并支持一日游跳过酒店", async () => {
    const interrupt: TravelInterrupt = {
      kind: "travel_selection",
      requires_hotel: false,
      self_arranged_allowed: true,
      outbound_options: [{
        option_id: "out-1",
        direction: "outbound",
        train_code: "G1",
        from_station: "上海虹桥",
        to_station: "杭州东",
        departure_time: "08:00",
        arrival_time: "08:45",
        seats: [{ name: "二等座", availability: "有票", price: 73 }],
      }],
      return_options: [{
        option_id: "return-1",
        direction: "return",
        train_code: "G2",
        from_station: "杭州东",
        to_station: "上海虹桥",
        departure_time: "19:00",
        arrival_time: "19:45",
        seats: [{ name: "二等座", availability: "有票", price: 73 }],
      }],
      hotel_options: [],
    };
    const wrapper = mount(InterruptPanel, {
      props: { interrupt, selection: {}, busy: false },
    });

    await wrapper.get('input[name="outbound"]').setValue();
    await wrapper.get('input[name="return"]').setValue();
    await wrapper.get(".interrupt-footer button").trigger("click");

    expect(wrapper.text()).toContain("本次无需选择住宿");
    expect(wrapper.emitted("resume")?.[0]).toEqual([{
      kind: "travel_selection",
      selection: {
        outbound: { option_id: "out-1", seat_type: "二等座" },
        return_trip: { option_id: "return-1", seat_type: "二等座" },
        hotel_id: null,
        self_arranged_outbound: false,
        self_arranged_return: false,
        self_arranged_hotel: false,
      },
    }]);
  });
});
