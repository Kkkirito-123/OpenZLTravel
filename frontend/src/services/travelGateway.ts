import { Client } from "@langchain/langgraph-sdk";

import type {
  ResumePayload,
  ThreadSnapshot,
  TravelState,
  TripRecord,
  TripSummary,
} from "../types";
import {
  firstUpdateNode,
  interruptFromTasks,
  isRecord,
  normalizeState,
  normalizeTripRecord,
  normalizeTripSummary,
  responseMessage,
} from "./travelGatewaySupport";

const GRAPH_ID = "travel";

export interface StreamCallbacks {
  onSnapshot: (snapshot: ThreadSnapshot) => void;
  onUpdate: (node: string) => void;
  onCursor: (runId: string | null, lastEventId: string | null) => void;
}

export type GraphRunRequest =
  | { input: { messages: Array<{ role: "user"; content: string }> } }
  | { resume: ResumePayload };

/**
 * 工作台依赖的最小远端能力。
 * 该边界只处理 LangGraph Thread/Run 与四个自定义 HTTP 接口，不保存任何业务会话副本。
 */
export interface TravelGateway {
  ensureIdentity(): Promise<void>;
  createThread(): Promise<string>;
  loadThread(threadId: string): Promise<ThreadSnapshot>;
  findActiveRun(threadId: string): Promise<string | null>;
  streamRun(
    threadId: string,
    request: GraphRunRequest,
    callbacks: StreamCallbacks,
  ): Promise<void>;
  reconnectRun(
    threadId: string,
    runId: string,
    lastEventId: string | null,
    callbacks: StreamCallbacks,
  ): Promise<void>;
  listTrips(): Promise<TripSummary[]>;
  getTrip(tripId: string): Promise<TripRecord>;
  deleteTrip(tripId: string): Promise<void>;
}

interface StreamEnvelope {
  id?: string;
  event: string;
  data: unknown;
}

/**
 * 基于官方 LangGraph JS SDK 的同源网关。
 * Run 使用可续传 SSE；断线时保留 run_id 与 Last-Event-ID，再通过 joinStream 继续消费，
 * 不轮询或创建第二套业务会话状态。
 */
export class LangGraphTravelGateway implements TravelGateway {
  private readonly apiUrl: string;
  private readonly client: Client<TravelState, Partial<TravelState>>;

  constructor(apiUrl = resolveApiUrl()) {
    this.apiUrl = apiUrl.replace(/\/$/, "");
    this.client = new Client<TravelState, Partial<TravelState>>({
      apiUrl: this.apiUrl,
      apiKey: null,
      timeoutMs: 20_000,
      onRequest: (_url, init) => ({ ...init, credentials: "include" }),
    });
  }

  async ensureIdentity(): Promise<void> {
    await this.request("/api/auth/anonymous", { method: "POST" });
  }

  async createThread(): Promise<string> {
    const thread = await this.client.threads.create({ graphId: GRAPH_ID });
    return thread.thread_id;
  }

  async loadThread(threadId: string): Promise<ThreadSnapshot> {
    const [thread, checkpoint] = await Promise.all([
      this.client.threads.get(threadId),
      this.client.threads.getState<TravelState>(threadId),
    ]);
    return {
      state: normalizeState(checkpoint.values),
      interrupt: interruptFromTasks(checkpoint.tasks),
      status: thread.status,
    };
  }

  async findActiveRun(threadId: string): Promise<string | null> {
    const runs = await this.client.runs.list(threadId, { limit: 10 });
    const active = runs.find((run) => run.status === "running" || run.status === "pending");
    return active?.run_id ?? null;
  }

  async streamRun(
    threadId: string,
    request: GraphRunRequest,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    let runId: string | null = null;
    const payload = {
      ...("resume" in request
        ? { command: { resume: request.resume } }
        : { input: request.input }),
      streamMode: ["values", "updates"] as Array<"values" | "updates">,
      streamResumable: true,
      streamIdleReconnect: "auto" as const,
      onDisconnect: "continue" as const,
      multitaskStrategy: "reject" as const,
      onRunCreated: ({ run_id }: { run_id: string }) => {
        runId = run_id;
        callbacks.onCursor(runId, null);
      },
    };
    const stream = this.client.runs.stream(threadId, GRAPH_ID, payload);
    await this.consume(stream, callbacks, () => runId);
    callbacks.onSnapshot(await this.loadThread(threadId));
  }

  async reconnectRun(
    threadId: string,
    runId: string,
    lastEventId: string | null,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    callbacks.onCursor(runId, lastEventId);
    const stream = this.client.runs.joinStream(threadId, runId, {
      lastEventId: lastEventId ?? undefined,
      streamMode: ["values", "updates"],
      streamIdleReconnect: "auto",
    });
    await this.consume(stream, callbacks, () => runId);
    callbacks.onSnapshot(await this.loadThread(threadId));
  }

  async listTrips(): Promise<TripSummary[]> {
    const payload = await this.request<unknown>("/api/trips");
    const values = Array.isArray(payload)
      ? payload
      : isRecord(payload) && Array.isArray(payload.items)
        ? payload.items
        : [];
    return values.flatMap((value) => {
      const summary = normalizeTripSummary(value);
      return summary ? [summary] : [];
    });
  }

  async getTrip(tripId: string): Promise<TripRecord> {
    const payload = await this.request<unknown>(`/api/trips/${encodeURIComponent(tripId)}`);
    return normalizeTripRecord(payload);
  }

  async deleteTrip(tripId: string): Promise<void> {
    await this.request(`/api/trips/${encodeURIComponent(tripId)}`, { method: "DELETE" });
  }

  private async consume(
    stream: AsyncIterable<StreamEnvelope>,
    callbacks: StreamCallbacks,
    currentRunId: () => string | null,
  ): Promise<void> {
    for await (const event of stream) {
      if (event.id) callbacks.onCursor(currentRunId(), event.id);
      if (event.event === "values") {
        callbacks.onSnapshot({
          state: normalizeState(event.data),
          interrupt: null,
          status: "busy",
        });
      }
      if (event.event === "updates") {
        const node = firstUpdateNode(event.data);
        if (node) callbacks.onUpdate(node);
      }
      if (event.event === "error") throw new Error(streamError(event.data));
    }
  }

  private async request<T = void>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.apiUrl}${path}`, {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init.headers },
    });
    if (!response.ok) throw new Error(await responseMessage(response));
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }
}

function resolveApiUrl(): string {
  const configured = import.meta.env.VITE_LANGGRAPH_API_URL as string | undefined;
  if (configured) return configured;
  return typeof window === "undefined" ? "http://127.0.0.1:2024" : window.location.origin;
}

function streamError(data: unknown): string {
  if (isRecord(data) && typeof data.message === "string") return data.message;
  return "旅行规划运行失败";
}
