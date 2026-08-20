import type {
  GraphNotice,
  TravelInterrupt,
  TravelState,
  TripRecord,
  TripSummary,
} from "../types";
import { createEmptyTravelState } from "../types";

/**
 * 把 Agent Server 的不可信 JSON 归一化为前端只读状态。
 * SDK 的运行时类型只能约束 TypeScript 调用方，不能约束网络响应；默认值和数组防御
 * 集中在这里，网关类本身只保留 Thread/Run 的远程编排。
 */
export function normalizeState(value: unknown): TravelState {
  if (!isRecord(value)) return createEmptyTravelState();
  return {
    ...createEmptyTravelState(),
    ...value,
    messages: Array.isArray(value.messages) ? value.messages : [],
    requirements: isRecord(value.requirements) ? value.requirements : {},
    destination_candidates: Array.isArray(value.destination_candidates)
      ? value.destination_candidates
      : [],
    facts: isRecord(value.facts) ? value.facts : {},
    selection: isRecord(value.selection) ? value.selection : {},
    warnings: noticeList(value.warnings),
    errors: noticeList(value.errors),
    revision_count: typeof value.revision_count === "number" ? value.revision_count : 0,
  } as TravelState;
}

/** 从任务列表中取出当前公开 interrupt；未知扩展类型会被安全忽略。 */
export function interruptFromTasks(
  tasks: Array<{ interrupts?: unknown[] }>,
): TravelInterrupt | null {
  for (const task of tasks) {
    for (const interrupt of task.interrupts ?? []) {
      const value = isRecord(interrupt) && "value" in interrupt ? interrupt.value : interrupt;
      if (isTravelInterrupt(value)) return value;
    }
  }
  return null;
}

/** updates 事件可能同时包含 interrupt 元数据，只取真实节点名供进度面板展示。 */
export function firstUpdateNode(data: unknown): string | null {
  if (!isRecord(data)) return null;
  return Object.keys(data).find((key) => key !== "__interrupt__") ?? null;
}

/** 把历史接口的宽松返回归一化为抽屉所需的最小摘要。 */
export function normalizeTripSummary(value: unknown): TripSummary | null {
  if (!isRecord(value) || typeof value.trip_id !== "string") return null;
  const requirements = isRecord(value.requirements) ? value.requirements : {};
  const city = isRecord(value.city) ? value.city : {};
  const draft = isRecord(value.draft) ? value.draft : {};
  const destination = value.destination ?? requirements.destination ?? city.name;
  if (typeof destination !== "string") return null;
  return {
    trip_id: value.trip_id,
    destination,
    start_date: optionalString(value.start_date ?? requirements.start_date),
    end_date: optionalString(value.end_date ?? requirements.end_date),
    summary: optionalString(value.summary ?? draft.summary) ?? undefined,
    created_at: optionalString(value.created_at) ?? undefined,
  };
}

/**
 * 校验历史详情的最小结构后再交给组件，避免网关中出现无约束的双重类型断言。
 * 领域字段的深度校验仍由后端负责；前端这里只确认展示层所需的骨架存在。
 */
export function normalizeTripRecord(value: unknown): TripRecord {
  const candidate = isRecord(value) && isRecord(value.trip) ? value.trip : value;
  if (!isTripRecord(candidate)) throw new Error("行程数据格式无效");
  return candidate;
}

/** 统一解析自定义 HTTP 接口的错误消息。 */
export async function responseMessage(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as unknown;
    if (isRecord(payload)) {
      if (typeof payload.detail === "string") return payload.detail;
      if (typeof payload.message === "string") return payload.message;
      if (isRecord(payload.error) && typeof payload.error.message === "string") {
        return payload.error.message;
      }
    }
  } catch {
    // 非 JSON 错误响应使用稳定的通用消息。
  }
  return `请求失败（${response.status}）`;
}

/** 判断网络载荷是否为普通 JSON 对象，排除 null 与数组。 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isTravelInterrupt(value: unknown): value is TravelInterrupt {
  return isRecord(value)
    && ["clarification", "destination_selection", "travel_selection"].includes(
      String(value.kind),
    );
}

function isTripRecord(value: unknown): value is TripRecord {
  if (!isRecord(value)) return false;
  if (typeof value.trip_id !== "string" || !isRecord(value.requirements)) return false;
  if (!isRecord(value.draft) || typeof value.draft.summary !== "string") return false;
  return Array.isArray(value.draft.days);
}

function noticeList(value: unknown): GraphNotice[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is GraphNotice => (
    isRecord(item)
    && typeof item.code === "string"
    && typeof item.message === "string"
    && typeof item.node === "string"
  ));
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
