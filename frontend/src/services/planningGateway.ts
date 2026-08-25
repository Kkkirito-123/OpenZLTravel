/**
 * TravelGraph 客户端。
 *
 * 只有本模块可以创建规划 Thread、启动 Run、恢复断线流和提交 route_preview。首次运行
 * 使用 Assistant 发来的 orderToken；后续恢复只发送受限的确认或修改命令，绝不把前端
 * 自己组装的需求、POI、票价或预算写入 Graph。
 */
import { Client } from "@langchain/langgraph-sdk";

import type {
  PlanningSnapshot,
  PlanningState,
  RoutePreviewInterrupt,
  TripRecord,
  TripSummary,
} from "../types";
import { emptyPlanningState } from "../types";

const GRAPH_ID = "travel";

export type PlanningRunRequest =
  | { orderToken: string }
  | { resume: { kind: "route_preview"; action: "confirm" | "message"; text?: string; allow_over_budget?: boolean } };

export interface PlanningCallbacks {
  onSnapshot: (snapshot: PlanningSnapshot) => void;
  onUpdate: (node: string) => void;
  onCursor: (runId: string | null, lastEventId: string | null) => void;
}

interface StreamEnvelope {
  id?: string;
  event: string;
  data: unknown;
}

/** 工单规划图、最终确认和历史行程的唯一客户端。 */
export class PlanningGateway {
  private readonly client = new Client<PlanningState, Partial<PlanningState>>({
    apiUrl: window.location.origin,
    apiKey: null,
    timeoutMs: 20_000,
    onRequest: (_url, init) => ({ ...init, credentials: "include" }),
  });

  async ensureIdentity(): Promise<void> {
    await this.request("/api/auth/anonymous", { method: "POST" });
  }

  async createThread(): Promise<string> {
    /** Thread 是规划阶段的新边界，不复用 Assistant 会话。 */
    return (await this.client.threads.create({ graphId: GRAPH_ID })).thread_id;
  }

  async loadThread(threadId: string): Promise<PlanningSnapshot> {
    const [thread, checkpoint] = await Promise.all([
      this.client.threads.get(threadId),
      this.client.threads.getState<PlanningState>(threadId),
    ]);
    return {
      state: normalizeState(checkpoint.values),
      interrupt: interruptFromTasks(checkpoint.tasks),
      status: thread.status,
    };
  }

  async findActiveRun(threadId: string): Promise<string | null> {
    const runs = await this.client.runs.list(threadId, { limit: 10 });
    return runs.find((run) => run.status === "running" || run.status === "pending")?.run_id ?? null;
  }

  async streamRun(
    threadId: string,
    request: PlanningRunRequest,
    callbacks: PlanningCallbacks,
  ): Promise<void> {
    // 首次运行传 order_token；route_preview 恢复只传 Command，避免重复提交工单。
    let runId: string | null = null;
    const stream = this.client.runs.stream(threadId, GRAPH_ID, {
      ...(requestIsResume(request)
        ? { command: { resume: request.resume } }
        : { input: { order_token: request.orderToken } }),
      streamMode: ["values", "updates"],
      streamResumable: true,
      streamIdleReconnect: "auto",
      onDisconnect: "continue",
      multitaskStrategy: "reject",
      onRunCreated: ({ run_id }: { run_id: string }) => {
        runId = run_id;
        callbacks.onCursor(runId, null);
      },
    });
    await this.consume(stream, callbacks, () => runId);
    callbacks.onSnapshot(await this.loadThread(threadId));
  }

  async reconnectRun(
    threadId: string,
    runId: string,
    lastEventId: string | null,
    callbacks: PlanningCallbacks,
  ): Promise<void> {
    // 断线恢复依赖 LangGraph 的 lastEventId，不重新创建 Run，也不重复保存行程。
    const stream = this.client.runs.joinStream(threadId, runId, {
      lastEventId: lastEventId ?? undefined,
      streamMode: ["values", "updates"],
      streamIdleReconnect: "auto",
    });
    await this.consume(stream, callbacks, () => runId);
    callbacks.onSnapshot(await this.loadThread(threadId));
  }

  async listTrips(): Promise<TripSummary[]> {
    const value = await this.request<unknown>("/api/trips");
    if (!Array.isArray(value)) return [];
    return value.filter(isRecord).flatMap((item) => typeof item.trip_id === "string" ? [{
      trip_id: item.trip_id,
      destination: String(item.destination ?? "未知目的地"),
      start_date: optionalString(item.start_date),
      end_date: optionalString(item.end_date),
      summary: optionalString(item.summary) ?? undefined,
      created_at: optionalString(item.created_at) ?? undefined,
    }] : []);
  }

  async getTrip(tripId: string): Promise<TripRecord> {
    return await this.request<TripRecord>(`/api/trips/${encodeURIComponent(tripId)}`);
  }

  async deleteTrip(tripId: string): Promise<void> {
    await this.request(`/api/trips/${encodeURIComponent(tripId)}`, { method: "DELETE" });
  }

  private async consume(
    stream: AsyncIterable<StreamEnvelope>,
    callbacks: PlanningCallbacks,
    currentRunId: () => string | null,
  ): Promise<void> {
    for await (const event of stream) {
      if (event.id) callbacks.onCursor(currentRunId(), event.id);
      if (event.event === "values") {
        callbacks.onSnapshot({ state: normalizeState(event.data), interrupt: null, status: "busy" });
      }
      if (event.event === "updates" && isRecord(event.data)) {
        const node = Object.keys(event.data).find((key) => key !== "__interrupt__");
        if (node) callbacks.onUpdate(node);
      }
      if (event.event === "error") throw new Error("旅行规划运行失败");
    }
  }

  private async request<T = void>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(path, {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init.headers },
    });
    if (!response.ok) throw new Error(`请求失败（${response.status}）`);
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  }
}

function requestIsResume(request: PlanningRunRequest): request is Extract<PlanningRunRequest, { resume: unknown }> {
  return "resume" in request;
}

function normalizeState(value: unknown): PlanningState {
  if (!isRecord(value)) return emptyPlanningState();
  return {
    ...emptyPlanningState(),
    ...value,
    facts: isRecord(value.facts) ? value.facts as unknown as PlanningState["facts"] : emptyPlanningState().facts,
    warnings: Array.isArray(value.warnings) ? value.warnings as PlanningState["warnings"] : [],
    errors: Array.isArray(value.errors) ? value.errors as PlanningState["errors"] : [],
  } as PlanningState;
}

function interruptFromTasks(tasks: Array<{ interrupts?: unknown[] }>): RoutePreviewInterrupt | null {
  for (const task of tasks) {
    for (const raw of task.interrupts ?? []) {
      const value = isRecord(raw) && "value" in raw ? raw.value : raw;
      if (isRecord(value) && value.kind === "route_preview" && typeof value.question === "string") {
        return value as unknown as RoutePreviewInterrupt;
      }
    }
  }
  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
