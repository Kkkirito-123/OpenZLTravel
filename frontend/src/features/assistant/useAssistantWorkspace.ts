/**
 * 唯一 AI 页面状态协调器。
 *
 * Assistant 状态和 Planning 状态分开保存：前者只保存公开快照与签名令牌，后者只保存
 * Thread/Run 游标和 Graph 展示状态。该 composable 负责交接、断线恢复和历史行程读取，
 * 不负责解析用户文本或决定旅行方案。
 */
import { computed, onMounted, ref } from "vue";

import {
  AssistantGateway,
  type AssistantTurn,
} from "./assistantGateway";
import {
  PlanningGateway,
  type PlanningCallbacks,
  type PlanningRunRequest,
} from "../planning/planningGateway";
import type {
  AssistantAction,
  AssistantHandoff,
  AssistantSnapshot,
  PlanningSnapshot,
  ToolEvent,
  TripRecord,
  TripSummary,
} from "../../types";
import { emptyAssistantSnapshot, emptyPlanningState } from "../../types";

const SESSION_TOKEN_KEY = "openzltravel.assistant.token";
const SESSION_SNAPSHOT_KEY = "openzltravel.assistant.snapshot";
const PLANNING_THREAD_KEY = "openzltravel.planning.thread";
const PLANNING_RUN_KEY = "openzltravel.planning.run";
const PLANNING_EVENT_KEY = "openzltravel.planning.event";

export function useAssistantWorkspace() {
  const assistantGateway = new AssistantGateway();
  const planningGateway = new PlanningGateway();
  const assistant = ref(readSnapshot());
  const sessionToken = ref(readText(SESSION_TOKEN_KEY));
  const planning = ref<PlanningSnapshot>({
    state: emptyPlanningState(),
    interrupt: null,
    status: "idle",
  });
  const threadId = ref(readText(PLANNING_THREAD_KEY));
  const runId = ref(readText(PLANNING_RUN_KEY));
  const lastEventId = ref(readText(PLANNING_EVENT_KEY));
  const activeNode = ref<string | null>(null);
  const tools = ref<ToolEvent[]>([]);
  const initializing = ref(true);
  const running = ref(false);
  const reconnecting = ref(false);
  const disconnected = ref(false);
  const error = ref("");
  const pendingUserMessage = ref("");
  const streamingReply = ref("");
  const lastTurn = ref<AssistantTurn | null>(null);
  const historyOpen = ref(false);
  const historyLoading = ref(false);
  const history = ref<TripSummary[]>([]);
  const finalTrip = ref<TripRecord | null>(null);
  const viewedTrip = ref<TripRecord | null>(null);

  const displayedTrip = computed(() => viewedTrip.value ?? finalTrip.value);
  const isPlanning = computed(() => Boolean(threadId.value) && planning.value.state.phase !== "completed");
  const canSend = computed(() => !running.value && !initializing.value && !viewedTrip.value);

  onMounted(initialize);

  async function initialize(): Promise<void> {
    initializing.value = true;
    try {
      await planningGateway.ensureIdentity();
      if (threadId.value) {
        planning.value = await planningGateway.loadThread(threadId.value);
        runId.value = await planningGateway.findActiveRun(threadId.value) ?? runId.value;
        if (runId.value && planning.value.status === "busy") await reconnectPlanning();
        await loadFinalTrip();
      }
      await refreshHistory();
    } catch (cause) {
      setError(cause, "无法恢复旅行会话");
    } finally {
      initializing.value = false;
    }
  }

  async function sendMessage(message: string): Promise<void> {
    const text = message.trim();
    if (!text || !canSend.value) return;
    if (planning.value.interrupt) {
      await resumePlanning({
        resume: { kind: "route_preview", action: "message", text },
      });
      return;
    }
    await runAssistantTurn({ message: text });
  }

  async function select(action: AssistantAction): Promise<void> {
    if (!canSend.value || planning.value.interrupt) return;
    await runAssistantTurn({ action });
  }

  async function runAssistantTurn(input: Omit<AssistantTurn, "session_token">): Promise<void> {
    // 每轮都携带最新签名快照；前端不回传自造的事实或选择作为权威状态。
    const request: AssistantTurn = {
      ...input,
      ...(sessionToken.value ? { session_token: sessionToken.value } : {}),
    };
    lastTurn.value = input;
    pendingUserMessage.value = input.message ?? actionLabel(input.action);
    streamingReply.value = "";
    tools.value = [];
    running.value = true;
    disconnected.value = false;
    error.value = "";
    let handoff: AssistantHandoff | null = null;
    try {
      await assistantGateway.turn(request, {
        onMessage: (content) => { streamingReply.value += content; },
        onToolStarted: markToolStarted,
        onToolResult: markToolCompleted,
        onSession: updateSession,
        onHandoff: (value) => { handoff = value; },
      });
      if (handoff) await startPlanning(handoff);
    } catch (cause) {
      disconnected.value = true;
      setError(cause, "Assistant SSE 已断开，可以重试这一轮");
    } finally {
      pendingUserMessage.value = "";
      streamingReply.value = "";
      running.value = false;
    }
  }

  async function retry(): Promise<void> {
    if (runId.value && threadId.value) {
      await reconnectPlanning();
      return;
    }
    if (lastTurn.value) await runAssistantTurn(lastTurn.value);
  }

  async function startPlanning(handoff: AssistantHandoff): Promise<void> {
    // Assistant 交接后创建新的 Graph Thread，两个服务不共享会话或 Checkpoint。
    await planningGateway.ensureIdentity();
    threadId.value = await planningGateway.createThread();
    writeText(PLANNING_THREAD_KEY, threadId.value);
    planning.value = { state: emptyPlanningState(), interrupt: null, status: "busy" };
    finalTrip.value = null;
    viewedTrip.value = null;
    await streamPlanning({ orderToken: handoff.order_token });
  }

  async function confirmPlanning(allowOverBudget = false): Promise<void> {
    await resumePlanning({
      resume: {
        kind: "route_preview",
        action: "confirm",
        ...(allowOverBudget ? { allow_over_budget: true } : {}),
      },
    });
  }

  async function resumePlanning(request: PlanningRunRequest): Promise<void> {
    if (!threadId.value || running.value) return;
    await streamPlanning(request);
  }

  async function streamPlanning(request: PlanningRunRequest): Promise<void> {
    if (!threadId.value) return;
    running.value = true;
    disconnected.value = false;
    error.value = "";
    planning.value = { ...planning.value, status: "busy" };
    try {
      await planningGateway.streamRun(
        threadId.value,
        request,
        planningCallbacks(),
      );
      await loadFinalTrip();
      await refreshHistory();
    } catch (cause) {
      disconnected.value = true;
      setError(cause, "规划流已断开，可以从当前 Run 继续接收");
    } finally {
      running.value = false;
    }
  }

  async function reconnectPlanning(): Promise<void> {
    if (!threadId.value || !runId.value) return;
    reconnecting.value = true;
    error.value = "";
    try {
      await planningGateway.reconnectRun(
        threadId.value,
        runId.value,
        lastEventId.value,
        planningCallbacks(),
      );
      disconnected.value = false;
      await loadFinalTrip();
      await refreshHistory();
    } catch (cause) {
      disconnected.value = true;
      setError(cause, "规划 Run 暂时无法恢复");
    } finally {
      reconnecting.value = false;
    }
  }

  function planningCallbacks(): PlanningCallbacks {
    return {
      onSnapshot: (snapshot) => { planning.value = snapshot; },
      onUpdate: (node) => { activeNode.value = node; },
      onCursor: (nextRunId, nextEventId) => {
        if (nextRunId) {
          runId.value = nextRunId;
          writeText(PLANNING_RUN_KEY, nextRunId);
        }
        if (nextEventId) {
          lastEventId.value = nextEventId;
          writeText(PLANNING_EVENT_KEY, nextEventId);
        }
      },
    };
  }

  async function loadFinalTrip(): Promise<void> {
    const tripId = planning.value.state.trip_id;
    if (planning.value.state.phase !== "completed" || !tripId) return;
    finalTrip.value = await planningGateway.getTrip(tripId);
    runId.value = null;
    lastEventId.value = null;
    removeText(PLANNING_RUN_KEY);
    removeText(PLANNING_EVENT_KEY);
  }

  async function refreshHistory(): Promise<void> {
    historyLoading.value = true;
    try {
      history.value = await planningGateway.listTrips();
    } finally {
      historyLoading.value = false;
    }
  }

  async function openHistory(): Promise<void> {
    historyOpen.value = true;
    await refreshHistory();
  }

  async function viewHistoricalTrip(tripId: string): Promise<void> {
    viewedTrip.value = await planningGateway.getTrip(tripId);
    historyOpen.value = false;
  }

  async function deleteHistoricalTrip(tripId: string): Promise<void> {
    await planningGateway.deleteTrip(tripId);
    if (viewedTrip.value?.trip_id === tripId) viewedTrip.value = null;
    await refreshHistory();
  }

  function returnToCurrentTrip(): void {
    viewedTrip.value = null;
  }

  function startNewTrip(): void {
    assistant.value = emptyAssistantSnapshot();
    planning.value = { state: emptyPlanningState(), interrupt: null, status: "idle" };
    sessionToken.value = null;
    threadId.value = null;
    runId.value = null;
    lastEventId.value = null;
    finalTrip.value = null;
    viewedTrip.value = null;
    tools.value = [];
    error.value = "";
    disconnected.value = false;
    for (const key of [
      SESSION_TOKEN_KEY,
      SESSION_SNAPSHOT_KEY,
      PLANNING_THREAD_KEY,
      PLANNING_RUN_KEY,
      PLANNING_EVENT_KEY,
    ]) removeText(key);
  }

  function updateSession(snapshot: AssistantSnapshot, token: string): void {
    assistant.value = snapshot;
    sessionToken.value = token;
    writeText(SESSION_TOKEN_KEY, token);
    writeText(SESSION_SNAPSHOT_KEY, JSON.stringify(snapshot));
  }

  function markToolStarted(name: string): void {
    tools.value = [...tools.value.filter((item) => item.name !== name), { name, status: "running" }];
  }

  function markToolCompleted(name: string, artifact?: string): void {
    tools.value = [
      ...tools.value.filter((item) => item.name !== name),
      { name, status: "completed", ...(artifact ? { artifact } : {}) },
    ];
  }

  function setError(cause: unknown, fallback: string): void {
    error.value = cause instanceof Error && cause.message ? cause.message : fallback;
  }

  return {
    assistant,
    planning,
    activeNode,
    tools,
    initializing,
    running,
    reconnecting,
    disconnected,
    error,
    pendingUserMessage,
    streamingReply,
    historyOpen,
    historyLoading,
    history,
    displayedTrip,
    isPlanning,
    canSend,
    sendMessage,
    select,
    retry,
    confirmPlanning,
    openHistory,
    refreshHistory,
    viewHistoricalTrip,
    deleteHistoricalTrip,
    returnToCurrentTrip,
    startNewTrip,
  };
}

function readSnapshot(): AssistantSnapshot {
  const raw = readText(SESSION_SNAPSHOT_KEY);
  if (!raw) return emptyAssistantSnapshot();
  try {
    return JSON.parse(raw) as AssistantSnapshot;
  } catch {
    return emptyAssistantSnapshot();
  }
}

function readText(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeText(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // 浏览器禁用会话存储时仍允许当前页面继续运行。
  }
}

function removeText(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // 同上。
  }
}

function actionLabel(action?: AssistantAction): string {
  if (!action) return "更新选择";
  return {
    select_destination: "选择目的地",
    select_attractions: "选择景点",
    select_outbound: "选择去程车次",
    select_return: "选择返程车次",
    select_hotel: "选择酒店",
    self_arrange: "选择自行安排",
  }[action.kind];
}
