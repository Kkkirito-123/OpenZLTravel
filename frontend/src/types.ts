/** TravelGraph 的公开阶段；阶段用于展示进度，前端不能据此自行决定下一节点。 */
export type TravelPhase =
  | "collecting"
  | "discovering"
  | "awaiting_selection"
  | "planning"
  | "reviewing"
  | "completed"
  | "failed"
  | "cancelled";

/** LangGraph SDK 返回的消息最小形状；content 保持 unknown，由展示层统一归一化。 */
export interface TravelMessage {
  id?: string;
  role?: "user" | "assistant" | "human" | "ai" | "system" | "tool";
  type?: string;
  content: unknown;
}

/** 当前 Thread 已收集的旅行需求；可选字段表示图仍可能处于 clarification 阶段。 */
export interface TravelRequirements {
  origin?: string | null;
  destination?: string | null;
  region?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  trip_days?: number | null;
  travelers?: number;
  budget?: number | null;
  pace?: "轻松" | "适中" | "紧凑";
  hotel_level?: "经济" | "舒适" | "品质";
  transport_mode?: "auto" | "walk" | "driving" | "transit" | "realtime_driving";
  preferences?: string[];
  dietary_preferences?: string[];
}

/** Catalog 或地图 Provider 确认的城市事实。 */
export interface CityFact {
  name: string;
  adcode?: string | null;
  latitude?: number | null;
  longitude?: number | null;
}

/** 保存行程时从 Provider 事实复制的地点快照，绝不使用 Planner 生成的名称。 */
export interface PlaceSnapshot {
  fact_id: string;
  name: string;
  address: string;
  category: "attraction" | "restaurant" | "hotel";
  latitude?: number | null;
  longitude?: number | null;
  image_url?: string | null;
}

/** 当前城市目录中的真实 POI；稳定 id 是 Planner 与前端展示之间的唯一引用键。 */
export interface CatalogPlace {
  id: string;
  name: string;
  address: string;
  category: "attraction" | "restaurant" | "hotel";
  latitude: number;
  longitude: number;
  image_url?: string | null;
}

/** 一个城市内按类别拆分的 Provider 候选池。 */
export interface CandidateCatalog {
  attractions: CatalogPlace[];
  restaurants: CatalogPlace[];
  hotels: CatalogPlace[];
}

/** 目的地推荐节点按确定性公式排序后公开的真实城市候选。 */
export interface DestinationCandidate {
  candidate_id: string;
  city: CityFact;
  score: number;
  reasons: string[];
  attraction_count?: number;
  restaurant_count?: number;
  hotel_count?: number;
}

/** 车次席别的真实可用性与报价；缺价时保持 null，前端显示“待确认”。 */
export interface RailSeat {
  name: string;
  availability?: string;
  price?: number | null;
}

/** 12306 Provider 返回的稳定车次事实，option_id 用于 interrupt resume。 */
export interface RailOption {
  option_id: string;
  direction: "outbound" | "return";
  travel_date?: string;
  train_code: string;
  from_station: string;
  to_station: string;
  departure_time: string;
  arrival_time: string;
  duration_minutes?: number;
  seats?: RailSeat[];
  price_from?: number | null;
  has_ticket?: boolean;
  is_transfer?: boolean;
  transfer_station?: string | null;
  booking_url?: string;
}

/** RollingGo 或目录降级返回的酒店事实，hotel_id 用于 interrupt resume。 */
export interface HotelOption {
  hotel_id: string;
  name: string;
  address?: string;
  latitude?: number | null;
  longitude?: number | null;
  star_rating?: number | null;
  price_per_night?: number | null;
  total_price?: number | null;
  distance_km?: number | null;
  image_url?: string | null;
  facilities?: string[];
  source?: "rollinggo" | "osm" | "amap" | "unknown";
  booking_url?: string | null;
}

/** 某天的天气事实；Provider 无法确认的字段保持 null，不使用经验值补齐。 */
export interface WeatherDay {
  date: string;
  day_weather?: string | null;
  night_weather?: string | null;
  day_temperature?: string | null;
  night_temperature?: string | null;
  warning?: string | null;
  source?: string | null;
}

/**
 * 图中全部 Provider 事实的前端投影。
 * 这些字段只读；组件只能展示或回传稳定 ID，不能把修改后的对象写回 Graph。
 */
export interface TravelFacts {
  city?: CityFact | null;
  catalog?: CandidateCatalog | null;
  outbound_options?: RailOption[];
  return_options?: RailOption[];
  hotel_options?: HotelOption[];
  weather?: WeatherDay[];
  routes?: Record<string, unknown[]>;
}

/** 用户选择的一趟车次及可选席别。 */
export interface RailChoice {
  option_id: string;
  seat_type?: string | null;
}

/** travel_selection 恢复后的结构化选择；事实 ID 与自行安排标志互斥关系由后端校验。 */
export interface TravelSelection {
  outbound?: RailChoice | null;
  return_trip?: RailChoice | null;
  hotel_id?: string | null;
  self_arranged_outbound?: boolean;
  self_arranged_return?: boolean;
  self_arranged_hotel?: boolean;
}

/** Planner 草稿中的单项活动，只携带 POI ID，名称和地址统一从 place_index 水合。 */
export interface ActivityDraft {
  poi_id: string;
  start_time: string;
  duration_minutes: number;
  note?: string;
}

/** Planner 草稿中的一天；餐饮和住宿同样只引用当前事实集合中的稳定 ID。 */
export interface DayDraft {
  day_index: number;
  theme: string;
  activities: ActivityDraft[];
  meal_ids?: string[];
  hotel_id?: string | null;
  notes?: string[];
}

/** PlannerAgent 的结构化草稿；在展示前已经过最终事实边界与日期结构校验。 */
export interface ItineraryDraft {
  summary: string;
  days: DayDraft[];
  tips?: string[];
}

/** 确定性预算节点的结果；只有 total_known 计入已经确认或明确标注的金额。 */
export interface BudgetBreakdown {
  intercity_transport?: number | null;
  local_transport?: number | null;
  hotel?: number | null;
  meals_estimated?: number;
  tickets_estimated?: number;
  total_known?: number;
  currency?: "CNY";
}

/** 最终校验后写入 `(user_id, "trips")` 的完整 Store 记录。 */
export interface TripRecord {
  trip_id: string;
  user_id?: string;
  requirements: TravelRequirements;
  city?: CityFact;
  selection?: TravelSelection;
  draft: ItineraryDraft;
  weather?: WeatherDay[];
  routes?: Record<string, unknown[]>;
  budget?: BudgetBreakdown;
  place_index?: Record<string, PlaceSnapshot>;
  warnings?: string[];
  created_at?: string;
}

/** 可跨 Checkpoint 稳定展示的告警或错误，不直接传输异常对象。 */
export interface GraphNotice {
  code: string;
  message: string;
  node: string;
}

/**
 * 前端读取的唯一权威执行状态。
 * 本地只保留 Thread ID、Run 游标和乐观消息；需求、事实、草稿与阶段始终以该快照为准。
 */
export interface TravelState {
  messages: TravelMessage[];
  phase: TravelPhase;
  requirements: TravelRequirements;
  destination_candidates: DestinationCandidate[];
  facts: TravelFacts;
  selection: TravelSelection;
  draft?: ItineraryDraft | null;
  review?: unknown;
  budget?: BudgetBreakdown | null;
  trip_id?: string | null;
  warnings: GraphNotice[];
  errors: GraphNotice[];
  revision_count: number;
}

/** 无效 resume 时随同类 interrupt 返回的稳定错误，收到它不代表图已推进。 */
export interface InterruptError {
  code: string;
  message: string;
}

/**
 * 需求字段不完整时的公开中断。
 * 前端必须把用户输入转换成 RequirementPatch，不能把自然语言字符串直接作为 resume。
 */
export interface ClarificationInterrupt {
  kind: "clarification";
  question: string;
  missing_fields: string[];
  error?: InterruptError | null;
}

/**
 * 未指定具体城市时的公开中断。
 * candidates 全部来自 Catalog 的确定性评分，恢复时只能回传其中的 candidate_id。
 */
export interface DestinationSelectionInterrupt {
  kind: "destination_selection";
  candidates: DestinationCandidate[];
  error?: InterruptError | null;
}

/**
 * Provider 事实准备完成后的公开中断。
 * 车票、酒店与价格均为只读事实；前端只提交稳定 ID 或明确的自行安排标记。
 */
export interface TravelSelectionInterrupt {
  kind: "travel_selection";
  outbound_options: RailOption[];
  return_options: RailOption[];
  hotel_options: HotelOption[];
  requires_hotel: boolean;
  self_arranged_allowed: boolean;
  error?: InterruptError | null;
}

export type TravelInterrupt =
  | ClarificationInterrupt
  | DestinationSelectionInterrupt
  | TravelSelectionInterrupt;

/** 与三类 interrupt 一一对应的 Command(resume=...) 载荷，kind 不匹配时不得推进图。 */
export type ResumePayload =
  | { kind: "clarification"; values: Partial<TravelRequirements> }
  | { kind: "destination_selection"; candidate_id: string }
  | { kind: "travel_selection"; selection: TravelSelection };

/** 一次 Thread 状态读取结果，合并 Checkpoint values 与当前任务中的 interrupt。 */
export interface ThreadSnapshot {
  state: TravelState;
  interrupt: TravelInterrupt | null;
  status: "idle" | "busy" | "interrupted" | "error";
}

/** 历史抽屉使用的轻量行程摘要，完整详情需要按 trip_id 单独读取。 */
export interface TripSummary {
  trip_id: string;
  destination: string;
  start_date?: string | null;
  end_date?: string | null;
  summary?: string;
  created_at?: string;
}

/**
 * 新建 Thread 或远端数据暂不可用时的状态模板。
 *
 * 这个常量只描述字段默认值；调用方应使用 ``createEmptyTravelState`` 获取独立数组，
 * 避免多个 Thread 共享同一个 messages、warnings 或 errors 引用。
 */
export const EMPTY_TRAVEL_STATE: TravelState = {
  messages: [],
  phase: "collecting",
  requirements: {},
  destination_candidates: [],
  facts: {},
  selection: {},
  warnings: [],
  errors: [],
  revision_count: 0,
};

/** 创建一份不会与其他 Thread 共享可变数组的初始状态。 */
export function createEmptyTravelState(): TravelState {
  return {
    ...EMPTY_TRAVEL_STATE,
    messages: [],
    requirements: {},
    destination_candidates: [],
    facts: {},
    selection: {},
    warnings: [],
    errors: [],
  };
}
