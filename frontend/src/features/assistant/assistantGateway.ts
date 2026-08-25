/**
 * Assistant SSE 客户端。
 *
 * 该客户端只负责传输和事件分发，不解析自然语言、不修改事实，也不生成规划状态。
 * session.updated 产生的签名快照交给 composable 保存；handoff.ready 交给页面启动一个
 * 全新的 LangGraph Thread/Run。这样前端不会把 Assistant 的临时状态直接写入 Graph。
 */
import type { AssistantAction, AssistantHandoff, AssistantSnapshot } from "../../types";

export interface AssistantCallbacks {
  onMessage: (content: string) => void;
  onToolStarted: (name: string) => void;
  onToolResult: (name: string, artifact?: string) => void;
  onSession: (snapshot: AssistantSnapshot, sessionToken: string) => void;
  onHandoff: (handoff: AssistantHandoff) => void;
}

export interface AssistantTurn {
  /** 每次请求只能提供 message 或 action 其中之一，可附带上一轮签名会话令牌。 */
  session_token?: string;
  message?: string;
  action?: AssistantAction;
}

/** 独立 Assistant Service 的最小 SSE 客户端。 */
export class AssistantGateway {
  /** 发送一轮对话，并按服务端事件顺序触发回调。 */
  async turn(request: AssistantTurn, callbacks: AssistantCallbacks): Promise<void> {
    const response = await fetch("/api/assistant/turn", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    if (!response.ok || !response.body) throw new Error(await responseText(response));
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) this.dispatch(block, callbacks);
      if (done) break;
    }
    if (buffer.trim()) this.dispatch(buffer, callbacks);
  }

  private dispatch(block: string, callbacks: AssistantCallbacks): void {
    const event = block.split("\n").find((line) => line.startsWith("event:"))?.slice(6).trim();
    const raw = block.split("\n").filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim()).join("\n");
    const data = raw ? JSON.parse(raw) as Record<string, unknown> : {};
    if (event === "message.delta" && typeof data.content === "string") callbacks.onMessage(data.content);
    if (event === "tool.started" && typeof data.name === "string") callbacks.onToolStarted(data.name);
    if (event === "tool.result" && typeof data.name === "string") {
      callbacks.onToolResult(data.name, typeof data.artifact === "string" ? data.artifact : undefined);
    }
    if (event === "session.updated" && typeof data.session_token === "string") {
      callbacks.onSession(data.snapshot as unknown as AssistantSnapshot, data.session_token);
    }
    if (event === "handoff.ready") callbacks.onHandoff(data as unknown as AssistantHandoff);
    if (event === "error") {
      throw new Error(typeof data.message === "string" ? data.message : "旅行助手暂时不可用");
    }
  }
}

async function responseText(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { detail?: string };
    return payload.detail ?? `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}
