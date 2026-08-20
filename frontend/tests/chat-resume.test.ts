import { describe, expect, it } from "vitest";

import { parseChatResume } from "../src/composables/chatResume";
import type {
  ClarificationInterrupt,
  DestinationSelectionInterrupt,
  TravelSelectionInterrupt,
} from "../src/types";

describe("parseChatResume", () => {
  it("把聊天中的完整补充需求转换为 clarification resume", () => {
    const interrupt: ClarificationInterrupt = {
      kind: "clarification",
      question: "请补充旅行信息。",
      missing_fields: ["origin", "destination_or_region", "start_date", "end_date"],
    };

    const result = parseChatResume(
      interrupt,
      "我从上海出发，2026-08-20 到 2026-08-23 去杭州玩，预算 5000 元，喜欢人文和美食",
    );

    expect(result.error).toBeUndefined();
    expect(result.payload).toEqual({
      kind: "clarification",
      values: {
        origin: "上海",
        destination: "杭州",
        start_date: "2026-08-20",
        end_date: "2026-08-23",
        budget: 5000,
        preferences: ["人文", "美食"],
      },
    });
  });

  it("支持按城市名或候选序号选择目的地", () => {
    const interrupt: DestinationSelectionInterrupt = {
      kind: "destination_selection",
      candidates: [
        {
          candidate_id: "destination:hangzhou",
          city: { name: "杭州" },
          score: 0.9,
          reasons: [],
        },
      ],
    };

    expect(parseChatResume(interrupt, "选杭州").payload).toEqual({
      kind: "destination_selection",
      candidate_id: "destination:hangzhou",
    });
    expect(parseChatResume(interrupt, "第 1 个").payload).toEqual({
      kind: "destination_selection",
      candidate_id: "destination:hangzhou",
    });
  });

  it("支持在聊天中同时选择酒店和自行安排去程", () => {
    const interrupt: TravelSelectionInterrupt = {
      kind: "travel_selection",
      outbound_options: [],
      return_options: [],
      hotel_options: [
        {
          hotel_id: "hotel:1",
          name: "湖畔酒店",
          address: "湖滨路 1 号",
        },
      ],
      requires_hotel: true,
      self_arranged_allowed: true,
    };

    const result = parseChatResume(interrupt, "酒店选第 1 个，去程自行安排，返程自行安排");

    expect(result.payload).toEqual({
      kind: "travel_selection",
      selection: {
        outbound: null,
        return_trip: null,
        hotel_id: "hotel:1",
        self_arranged_outbound: true,
        self_arranged_return: true,
        self_arranged_hotel: false,
      },
    });
  });

  it("无法识别当前候选时返回稳定错误", () => {
    const interrupt: DestinationSelectionInterrupt = {
      kind: "destination_selection",
      candidates: [],
    };

    expect(parseChatResume(interrupt, "随便吧").error).toContain("候选城市名称");
  });
});
