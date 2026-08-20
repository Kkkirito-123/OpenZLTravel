import { computed, onMounted, ref } from "vue";

import {
  LangGraphTravelGateway,
  type GraphRunRequest,
  type StreamCallbacks,
  type TravelGateway,
} from "../services/travelGateway";
import type {
  ResumePayload,
  ThreadSnapshot,
  TravelMessage,
  TravelState,
  TripRecord,
  TripSummary,
} from "../types";
import { createEmptyTravelState } from "../types";
import {
  draftTripFromState,
  errorText,
  normalizeMessages,
  resolveStorage,
  type ConversationMessage,
  type StorageLike,
} from "./travelThreadSupport";
import { parseChatResume } from "./chatResume";

export type { ConversationMessage } from "./travelThreadSupport";

const THREAD_STORAGE_KEY = "openzltravel.travel_thread_id";

interface TravelThreadOptions {
  gateway?: TravelGateway;
  storage?: StorageLike;
  autoInitialize?: boolean;
}
/**
 * 单一旅行工作台状态入口。
 *
 * 这里集中管理当前 Thread ID、权威 TravelState、流式 Run 游标、interrupt/resume、
 * 断线恢复与 Store 行程历史；页面和子组件只消费这些 ref，不各自复制远端状态。
 */
export function useTravelThread(options: TravelThreadOptions = {}) {
  const gateway = options.gateway ?? new LangGraphTravelGateway();
  const storage = options.storage ?? resolveStorage();
  const threadId = ref<string | null>(null);
  const state = ref<TravelState>(createEmptyTravelState());
  const interrupt = ref<ThreadSnapshot["interrupt"]>(null);
  const activeNode = ref<string | null>(null);
  const activeRunId = ref<string | null>(null);
  const lastEventId = ref<string | null>(null);
  const initializing = ref(false);
  const running = ref(false);
  const reconnecting = ref(false);
  const disconnected = ref(false);
  const error = ref("");
  const optimisticMessages = ref<ConversationMessage[]>([]);
  const history = ref<TripSummary[]>([]);
  const historyLoading = ref(false);
  const historyOpen = ref(false);
  const viewedTrip = ref<TripRecord | null>(null);
  let initializePromise: Promise<void> | null = null;

  const messages = computed(() => [
    ...normalizeMessages(state.value.messages),
    ...optimisticMessages.value,
  ]);
  const currentTrip = computed<TripRecord | null>(() => {
    if (viewedTrip.value) return viewedTrip.value;
    return draftTripFromState(state.value);
  });
  const canSendMessage = computed(
    () => !initializing.value && !running.value,
  );

  /** 初始化匿名身份并恢复本地记录的唯一 Thread；无可用 Thread 时才创建新的。 */
  async function initialize(): Promise<void> {
    if (initializePromise) return initializePromise;
    initializePromise = doInitialize().finally(() => {
      initializePromise = null;
    });
    return initializePromise;
  }

  async function doInitialize(): Promise<void> {
    initializing.value = true;
    error.value = "";
    try {
      await gateway.ensureIdentity();
      const savedThreadId = storage.getItem(THREAD_STORAGE_KEY);
      if (savedThreadId && await restoreThread(savedThreadId)) return;
      await createFreshThread();
    } catch (cause) {
      error.value = errorText(cause, "无法连接旅行服务，请确认 Agent Server 已启动。");
    } finally {
      initializing.value = false;
    }
  }

  async function restoreThread(savedThreadId: string): Promise<boolean> {
    try {
      threadId.value = savedThreadId;
      applySnapshot(await gateway.loadThread(savedThreadId));
      if (state.value.trip_id) await loadCompletedTrip(state.value.trip_id);
      const runId = await gateway.findActiveRun(savedThreadId);
      if (runId) {
        activeRunId.value = runId;
        await reconnect();
      }
      return true;
    } catch {
      storage.removeItem(THREAD_STORAGE_KEY);
      threadId.value = null;
      return false;
    }
  }

  async function createFreshThread(): Promise<void> {
    const createdId = await gateway.createThread();
    threadId.value = createdId;
    storage.setItem(THREAD_STORAGE_KEY, createdId);
    state.value = createEmptyTravelState();
    interrupt.value = null;
    viewedTrip.value = null;
    activeNode.value = null;
    activeRunId.value = null;
    lastEventId.value = null;
    optimisticMessages.value = [];
    disconnected.value = false;
  }

  async function submitMessage(content: string): Promise<void> {
    const text = content.trim();
    if (!text || running.value) return;
    if (!threadId.value) await initialize();
    if (!threadId.value) return;
    if (interrupt.value) {
      const result = parseChatResume(interrupt.value, text);
      if (!result.payload) {
        error.value = result.error || "这句话还不能完成当前确认，请参考候选卡片。";
        return;
      }
      // resume 本身不会向 LangGraph messages 写入自然语言，因此保留这条确认消息
      // 作为工作台展示记录；稳定事实仍只来自后端重新校验后的 interrupt 结果。
      optimisticMessages.value.push({
        id: `resume-${Date.now()}`,
        role: "user",
        text,
        pending: false,
      });
      await execute({ resume: result.payload });
      return;
    }
    optimisticMessages.value.push({
      id: `pending-${Date.now()}`,
      role: "user",
      text,
      pending: true,
    });
    await execute({ input: { messages: [{ role: "user", content: text }] } });
  }

  /** 只接受与当前 interrupt.kind 完全一致的恢复载荷，客户端预校验失败时不发起 Run。 */
  async function resume(payload: ResumePayload): Promise<void> {
    if (!interrupt.value) {
      error.value = "当前没有等待恢复的旅行步骤。";
      return;
    }
    if (interrupt.value.kind !== payload.kind) {
      error.value = "恢复数据与当前步骤不匹配，请刷新后重试。";
      return;
    }
    await execute({ resume: payload });
  }

  async function execute(request: GraphRunRequest): Promise<void> {
    if (!threadId.value || running.value) return;
    running.value = true;
    disconnected.value = false;
    error.value = "";
    interrupt.value = null;
    try {
      await gateway.streamRun(threadId.value, request, streamCallbacks());
      activeRunId.value = null;
      lastEventId.value = null;
      await refreshCompletedTrip();
    } catch (cause) {
      disconnected.value = Boolean(activeRunId.value);
      error.value = errorText(
        cause,
        disconnected.value
          ? "流式连接已中断，任务仍在后台运行，可以继续接收。"
          : "旅行规划暂时失败，请重试。",
      );
    } finally {
      running.value = false;
    }
  }

  /** 使用已记录的 run_id 与事件游标继续可续传流，避免重复启动导致重复保存。 */
  async function reconnect(): Promise<void> {
    if (!threadId.value || reconnecting.value) return;
    reconnecting.value = true;
    error.value = "";
    try {
      const runId = activeRunId.value ?? await gateway.findActiveRun(threadId.value);
      if (!runId) {
        applySnapshot(await gateway.loadThread(threadId.value));
        disconnected.value = false;
        return;
      }
      activeRunId.value = runId;
      await gateway.reconnectRun(
        threadId.value,
        runId,
        lastEventId.value,
        streamCallbacks(),
      );
      activeRunId.value = null;
      lastEventId.value = null;
      disconnected.value = false;
      await refreshCompletedTrip();
    } catch (cause) {
      disconnected.value = true;
      error.value = errorText(cause, "暂时无法恢复流，请稍后重试。");
    } finally {
      reconnecting.value = false;
    }
  }

  /** 创建全新 Thread 并清空本地展示状态；历史行程仍由 Store 独立保留。 */
  async function startNewTrip(): Promise<void> {
    if (running.value || reconnecting.value) return;
    error.value = "";
    try {
      await createFreshThread();
    } catch (cause) {
      error.value = errorText(cause, "无法创建新旅行，请稍后重试。");
    }
  }

  async function openHistory(): Promise<void> {
    historyOpen.value = true;
    await refreshHistory();
  }

  function closeHistory(): void {
    historyOpen.value = false;
  }

  async function refreshHistory(): Promise<void> {
    historyLoading.value = true;
    try {
      history.value = await gateway.listTrips();
      error.value = "";
    } catch (cause) {
      error.value = errorText(cause, "无法读取历史行程。");
    } finally {
      historyLoading.value = false;
    }
  }

  async function viewHistoricalTrip(tripId: string): Promise<void> {
    try {
      viewedTrip.value = await gateway.getTrip(tripId);
      historyOpen.value = false;
      error.value = "";
    } catch (cause) {
      error.value = errorText(cause, "无法读取该行程。");
    }
  }

  async function deleteHistoricalTrip(tripId: string): Promise<void> {
    try {
      await gateway.deleteTrip(tripId);
      history.value = history.value.filter((item) => item.trip_id !== tripId);
      if (viewedTrip.value?.trip_id === tripId) viewedTrip.value = null;
      error.value = "";
    } catch (cause) {
      error.value = errorText(cause, "无法删除该行程。");
    }
  }

  function returnToCurrentTrip(): void {
    viewedTrip.value = null;
  }

  function streamCallbacks(): StreamCallbacks {
    return {
      onSnapshot: applySnapshot,
      onUpdate: (node) => {
        activeNode.value = node;
      },
      onCursor: (runId, eventId) => {
        if (runId) activeRunId.value = runId;
        if (eventId) lastEventId.value = eventId;
      },
    };
  }

  function applySnapshot(snapshot: ThreadSnapshot): void {
    state.value = snapshot.state;
    interrupt.value = snapshot.interrupt;
    if (snapshot.interrupt?.error?.message) error.value = snapshot.interrupt.error.message;
    reconcileOptimisticMessages(snapshot.state.messages);
  }

  function reconcileOptimisticMessages(authoritative: TravelMessage[]): void {
    const userTexts = normalizeMessages(authoritative)
      .filter((message) => message.role === "user")
      .map((message) => message.text);
    optimisticMessages.value = optimisticMessages.value.filter(
      (message) => !userTexts.includes(message.text),
    );
  }

  async function refreshCompletedTrip(): Promise<void> {
    if (state.value.trip_id) await loadCompletedTrip(state.value.trip_id);
  }

  async function loadCompletedTrip(tripId: string): Promise<void> {
    try {
      viewedTrip.value = await gateway.getTrip(tripId);
    } catch {
      // Store 写入与流结束可能存在极短竞态，当前 state 中的 draft 仍可展示。
    }
  }

  if (options.autoInitialize !== false) onMounted(() => void initialize());

  return {
    threadId,
    state,
    interrupt,
    activeNode,
    initializing,
    running,
    reconnecting,
    disconnected,
    error,
    messages,
    currentTrip,
    canSendMessage,
    history,
    historyLoading,
    historyOpen,
    viewedTrip,
    initialize,
    submitMessage,
    resume,
    reconnect,
    startNewTrip,
    openHistory,
    closeHistory,
    refreshHistory,
    viewHistoricalTrip,
    deleteHistoricalTrip,
    returnToCurrentTrip,
  };
}
