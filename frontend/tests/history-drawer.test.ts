import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import { nextTick } from "vue";

import HistoryDrawer from "../src/components/HistoryDrawer.vue";

describe("HistoryDrawer", () => {
  it("支持打开行程、二次确认删除和开始新行程", async () => {
    const wrapper = mount(HistoryDrawer, {
      props: {
        open: true,
        loading: false,
        items: [{
          trip_id: "trip-1",
          destination: "杭州",
          start_date: "2026-10-01",
          end_date: "2026-10-03",
          summary: "西湖与人文三日游",
        }],
      },
      attachTo: document.body,
    });

    await wrapper.get(".history-main").trigger("click");
    expect(wrapper.emitted("select")?.[0]).toEqual(["trip-1"]);

    await wrapper.get('[aria-label="删除 杭州 行程"]').trigger("click");
    expect(wrapper.text()).toContain("确认删除");
    const deleteButton = wrapper.findAll(".delete-confirm button").at(1);
    await deleteButton?.trigger("click");
    expect(wrapper.emitted("delete")?.[0]).toEqual(["trip-1"]);

    await wrapper.get(".drawer-new-button").trigger("click");
    expect(wrapper.emitted("newTrip")).toHaveLength(1);
    wrapper.unmount();
  });

  it("按 Escape 关闭抽屉", async () => {
    const wrapper = mount(HistoryDrawer, {
      props: { open: true, loading: false, items: [] },
      attachTo: document.body,
    });

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await nextTick();

    expect(wrapper.emitted("close")).toHaveLength(1);
    wrapper.unmount();
  });
});
