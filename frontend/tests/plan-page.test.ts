// @vitest-environment jsdom
import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PlanPage from "../src/pages/PlanPage.vue";

const api = vi.hoisted(() => ({
  createPlanningSession: vi.fn(),
  errorMessage: vi.fn(() => "请求失败，请重试"),
}));
const push = vi.hoisted(() => vi.fn());

vi.mock("../src/api", () => api);
vi.mock("vue-router", () => ({
  useRouter: () => ({ push }),
}));

beforeEach(() => {
  api.createPlanningSession.mockReset();
  api.errorMessage.mockClear();
  push.mockReset();
});

describe("PlanPage", () => {
  it("偏好选项使用可访问的选中状态", async () => {
    const wrapper = mount(PlanPage);
    const scenery = wrapper.get('button[aria-pressed="true"]');

    expect(scenery.text()).toBe("自然风景");
    await scenery.trigger("click");

    expect(scenery.attributes("aria-pressed")).toBe("false");
  });
});
