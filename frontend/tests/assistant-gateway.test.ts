import { afterEach, describe, expect, it, vi } from "vitest";

import { AssistantGateway } from "../src/features/assistant/assistantGateway";
import { emptyAssistantSnapshot } from "../src/types";

afterEach(() => vi.unstubAllGlobals());

describe("AssistantGateway", () => {
  it("按 SSE 事件分发消息、工具、会话和工单", async () => {
    const snapshot = emptyAssistantSnapshot();
    const body = [
      "event: tool.started\ndata: {\"name\":\"search_pois\"}\n\n",
      "event: tool.result\ndata: {\"name\":\"search_pois\",\"artifact\":\"pois\"}\n\n",
      "event: message.delta\ndata: {\"content\":\"请选择景点\"}\n\n",
      `event: session.updated\ndata: ${JSON.stringify({ snapshot, session_token: "token" })}\n\n`,
      `event: handoff.ready\ndata: ${JSON.stringify({ order: {}, order_token: "order-token" })}\n\n`,
      "event: done\ndata: {}\n\n",
    ].join("");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    })));
    const events: string[] = [];

    await new AssistantGateway().turn({ message: "去杭州" }, {
      onMessage: (content) => events.push(`message:${content}`),
      onToolStarted: (name) => events.push(`start:${name}`),
      onToolResult: (name, artifact) => events.push(`result:${name}:${artifact}`),
      onSession: (_value, token) => events.push(`session:${token}`),
      onHandoff: (handoff) => events.push(`handoff:${handoff.order_token}`),
    });

    expect(events).toEqual([
      "start:search_pois",
      "result:search_pois:pois",
      "message:请选择景点",
      "session:token",
      "handoff:order-token",
    ]);
  });
});
