import type {
  ItineraryDraft,
  PlaceSnapshot,
  TravelFacts,
  TravelMessage,
  TravelState,
  TripRecord,
} from "../types";

/** 对话区域使用的稳定展示模型，不把 LangChain 消息实现泄漏给组件。 */
export interface ConversationMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  pending: boolean;
}

/** 仅包含 Thread ID 持久化所需能力，测试可注入内存实现。 */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 把 Agent Server 的多种消息表示收敛为工作台只读消息。 */
export function normalizeMessages(values: TravelMessage[]): ConversationMessage[] {
  return values.flatMap((message, index) => {
    const role = normalizeRole(message.role ?? message.type);
    const text = messageText(message.content);
    if (!role || !text) return [];
    return [{
      id: message.id ?? `${role}-${index}-${text.slice(0, 12)}`,
      role,
      text,
      pending: false,
    }];
  });
}

/**
 * 执行中的草稿尚未写入 Store 时，合并 Catalog 与实时酒店事实构造临时展示索引。
 * RollingGo 酒店可能不在本地 Catalog 中，因此不能只索引 catalog.hotels。
 * 完成后的历史行程优先使用后端保存的 TripRecord.place_index。
 */
export function placeIndexFromFacts(
  facts: TravelFacts,
): Record<string, PlaceSnapshot> {
  const catalog = facts.catalog;
  const values = Object.fromEntries(
    (catalog ? [...catalog.attractions, ...catalog.restaurants, ...catalog.hotels] : []).map((place) => [
      place.id,
      {
        fact_id: place.id,
        name: place.name,
        address: place.address,
        category: place.category,
        latitude: place.latitude,
        longitude: place.longitude,
        image_url: place.image_url,
      },
    ]),
  ) as Record<string, PlaceSnapshot>;
  for (const hotel of facts.hotel_options ?? []) {
    values[hotel.hotel_id] = {
      fact_id: hotel.hotel_id,
      name: hotel.name,
      address: hotel.address ?? "",
      category: "hotel",
      latitude: hotel.latitude,
      longitude: hotel.longitude,
      image_url: hotel.image_url,
    };
  }
  return values;
}

/**
 * 把当前 State 中尚未保存的草稿投影成行程展示模型。
 *
 * 这是纯函数而不是 Thread 副作用：组件需要的地点名称仍从 Provider facts 水合，
 * 不信任 Planner 草稿里的展示文案。已保存行程的完整 ``place_index`` 由调用方优先
 * 使用，因此该函数只处理“当前执行中的草稿”这一种情况。
 */
export function draftTripFromState(state: TravelState): TripRecord | null {
  const draft: ItineraryDraft | null | undefined = state.draft;
  if (!draft) return null;
  return {
    trip_id: state.trip_id ?? "draft",
    requirements: state.requirements,
    city: state.facts.city ?? undefined,
    selection: state.selection,
    draft,
    weather: state.facts.weather ?? [],
    routes: state.facts.routes ?? {},
    budget: state.budget ?? undefined,
    place_index: placeIndexFromFacts(state.facts),
    warnings: state.warnings.map((notice) => notice.message),
  };
}

/** 浏览器使用 localStorage，SSR/单元测试环境使用最小内存替代。 */
export function resolveStorage(): StorageLike {
  if (typeof window !== "undefined") return window.localStorage;
  const values = new Map<string, string>();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => void values.set(key, value),
    removeItem: (key) => void values.delete(key),
  };
}

/** 保留远端稳定错误消息；非 Error 异常使用当前操作的中文兜底。 */
export function errorText(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback;
}

function normalizeRole(value: string | undefined): "user" | "assistant" | null {
  if (value === "user" || value === "human") return "user";
  if (value === "assistant" || value === "ai") return "assistant";
  return null;
}

function messageText(content: unknown): string {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content.map((item) => {
    if (typeof item === "string") return item;
    if (typeof item === "object" && item && "text" in item && typeof item.text === "string") {
      return item.text;
    }
    return "";
  }).join("").trim();
}
