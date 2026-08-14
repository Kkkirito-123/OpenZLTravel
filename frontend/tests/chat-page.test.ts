// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ChatPage from "../src/pages/ChatPage.vue";
import type {
  AssistantSessionView,
  AssistantTurnResponse,
  TravelDialogueState,
} from "../src/types";

const api = vi.hoisted(() => ({
  createAssistantSession: vi.fn(),
  deleteAssistantMemory: vi.fn(),
  errorMessage: vi.fn(() => "意图识别超时，请重试"),
  getAssistantSession: vi.fn(),
  listAssistantMemories: vi.fn(),
  sendAssistantMessage: vi.fn(),
}));
const routeParams = vi.hoisted(() => ({ sessionId: "session-1" as string | undefined }));
const replace = vi.hoisted(() => vi.fn());

vi.mock("../src/api", () => api);
vi.mock("vue-router", () => ({
  RouterLink: { template: "<a><slot /></a>" },
  useRoute: () => ({ params: routeParams }),
  useRouter: () => ({ replace }),
}));

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset());
  api.errorMessage.mockReturnValue("意图识别超时，请重试");
  api.listAssistantMemories.mockResolvedValue([]);
  routeParams.sessionId = "session-1";
  replace.mockReset();
});

describe("ChatPage", () => {
  it("刷新后恢复完整轮次和当前需求", async () => {
    api.getAssistantSession.mockResolvedValue(sampleView());

    const wrapper = mount(ChatPage);
    await flushPromises();

    expect(api.getAssistantSession).toHaveBeenCalledWith("session-1");
    expect(wrapper.text()).toContain("我想去广西看历史景点");
    expect(wrapper.text()).toContain("计划玩几天");
    expect(wrapper.text()).toContain("广西壮族自治区");
    expect(wrapper.text()).toContain("还需确认");
    expect(wrapper.text()).toContain("目的地需求发现");
    expect(wrapper.text()).toContain("360");
  });

  it("发送后更新状态并展示规划会话入口", async () => {
    api.getAssistantSession.mockResolvedValue(sampleView());
    const response = sampleResponse();
    api.sendAssistantMessage.mockResolvedValue(response);
    const wrapper = mount(ChatPage);
    await flushPromises();

    await wrapper.get("#assistant-message").setValue("三天，预算两千元");
    await wrapper.get(".chat-composer").trigger("submit");
    await flushPromises();

    expect(api.sendAssistantMessage).toHaveBeenCalledWith(
      "session-1",
      expect.objectContaining({ content: "三天，预算两千元" }),
    );
    expect(wrapper.text()).toContain("旅行需求已经完整");
    expect(wrapper.text()).toContain("2000 元");
    expect(wrapper.text()).toContain("查看车票与酒店");
  });

  it("失败后使用同一个 message_id 重试", async () => {
    api.getAssistantSession.mockResolvedValue(sampleView());
    api.sendAssistantMessage
      .mockRejectedValueOnce(new Error("timeout"))
      .mockResolvedValueOnce(sampleResponse());
    const wrapper = mount(ChatPage);
    await flushPromises();

    await wrapper.get("#assistant-message").setValue("三天，预算两千元");
    await wrapper.get(".chat-composer").trigger("submit");
    await flushPromises();
    expect(wrapper.text()).toContain("意图识别超时，请重试");

    await wrapper.get(".chat-error button").trigger("click");
    await flushPromises();

    const first = api.sendAssistantMessage.mock.calls[0][1];
    const second = api.sendAssistantMessage.mock.calls[1][1];
    expect(second.message_id).toBe(first.message_id);
    expect(wrapper.find(".chat-error").exists()).toBe(false);
  });

  it("首页创建会话后切换到可恢复地址", async () => {
    routeParams.sessionId = undefined;
    api.createAssistantSession.mockResolvedValue(sampleView());

    mount(ChatPage);
    await flushPromises();

    expect(api.createAssistantSession).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith("/chat/session-1");
  });

  it("展示并删除明确保存的长期偏好", async () => {
    const view = sampleView();
    view.memories = [
      {
        key: "origin",
        value: "杭州",
        version: 1,
        source_session_id: "session-0",
        created_at: "2026-08-14T08:00:00Z",
        updated_at: "2026-08-14T08:00:00Z",
      },
    ];
    api.getAssistantSession.mockResolvedValue(view);
    api.deleteAssistantMemory.mockResolvedValue(undefined);
    const wrapper = mount(ChatPage);
    await flushPromises();

    expect(wrapper.text()).toContain("常用出发地");
    expect(wrapper.text()).toContain("杭州");
    await wrapper.get('.assistant-memory button[aria-label="删除常用出发地"]').trigger("click");
    await flushPromises();

    expect(api.deleteAssistantMemory).toHaveBeenCalledWith("origin");
    expect(wrapper.text()).toContain("暂无长期偏好");
  });
});

function sampleView(): AssistantSessionView {
  return {
    state: sampleState(),
    skill: {
      id: "destination_discovery",
      title: "目的地需求发现",
      description: "整理旅行需求",
      required_slots: ["preferences", "days", "budget"],
      effect: "collect_requirements",
    },
    memories: [],
    turns: [
      {
        sequence: 1,
        user_content: "我想去广西看历史景点",
        assistant_content: "计划玩几天？大概预算是多少？",
        created_at: "2026-08-14T08:00:00Z",
      },
    ],
  };
}

function sampleResponse(): AssistantTurnResponse {
  const state = sampleState();
  state.revision = 2;
  state.status = "planning_started";
  state.pending_slots = [];
  state.planning_session_id = "planning-1";
  state.slots.days = 3;
  state.slots.budget = 2000;
  state.slot_metadata.days = { source: "user_explicit", updated_turn: 2 };
  state.slot_metadata.budget = { source: "user_explicit", updated_turn: 2 };
  return {
    message_id: "message-2",
    reply: "旅行需求已经完整，正在查询车票、酒店、天气和当地信息。",
    state,
    missing_slots: [],
    planning_session_id: "planning-1",
    skill: {
      id: "destination_discovery",
      title: "目的地需求发现",
      description: "整理旅行需求",
      required_slots: [],
      effect: "collect_requirements",
    },
    command_source: "fast_parser",
    context_tokens: 0,
  };
}

function sampleState(): TravelDialogueState {
  return {
    session_id: "session-1",
    revision: 1,
    status: "collecting",
    active_flow: "destination_discovery",
    slots: {
      origin: null,
      destination_region: "广西壮族自治区",
      destination_city: null,
      start_date: null,
      end_date: null,
      days: null,
      budget: null,
      travelers: 1,
      preferences: ["历史景点"],
      dietary_preferences: [],
      distance_preference: null,
      pace: "适中",
      hotel_level: "舒适",
      transport_mode: "auto",
      notes: "",
    },
    slot_metadata: {
      destination_region: { source: "user_explicit", updated_turn: 1 },
      preferences: { source: "user_explicit", updated_turn: 1 },
    },
    pending_slots: ["days", "budget"],
    last_question: "计划玩几天？大概预算是多少？",
    planning_session_id: null,
    token_usage: {
      model_calls: 1,
      input_tokens: 320,
      output_tokens: 40,
      cached_input_tokens: 0,
      total_tokens: 360,
    },
    created_at: "2026-08-14T08:00:00Z",
    updated_at: "2026-08-14T08:00:00Z",
  };
}
