import { describe, expect, it } from "vitest";

import {
  interruptFromTasks,
  normalizeState,
  normalizeTripRecord,
  normalizeTripSummary,
} from "../src/services/travelGatewaySupport";

describe("travelGatewaySupport", () => {
  it("把不完整 Checkpoint 归一化为可安全读取的 TravelState", () => {
    const state = normalizeState({
      phase: "planning",
      messages: "invalid",
      warnings: [
        { code: "ok", message: "可展示", node: "planner_agent" },
        { message: "缺少稳定字段" },
      ],
    });

    expect(state.phase).toBe("planning");
    expect(state.messages).toEqual([]);
    expect(state.requirements).toEqual({});
    expect(state.warnings).toEqual([
      { code: "ok", message: "可展示", node: "planner_agent" },
    ]);
  });

  it("从任务列表提取已登记的 interrupt，忽略未知类型", () => {
    expect(interruptFromTasks([
      { interrupts: [{ value: { kind: "future_interrupt" } }] },
      {
        interrupts: [{
          value: {
            kind: "clarification",
            question: "何时出发？",
            missing_fields: ["start_date"],
          },
        }],
      },
    ])?.kind).toBe("clarification");
  });

  it("校验历史详情骨架，并兼容包装响应与摘要投影", () => {
    const record = normalizeTripRecord({
      trip: {
        trip_id: "trip-1",
        requirements: { destination: "杭州" },
        draft: { summary: "杭州行程", days: [] },
      },
    });

    expect(record.trip_id).toBe("trip-1");
    expect(normalizeTripSummary(record)?.destination).toBe("杭州");
    expect(() => normalizeTripRecord({ trip_id: "broken" })).toThrow("行程数据格式无效");
  });
});
