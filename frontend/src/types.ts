// 与后端响应一一对应的稳定类型；页面状态保留在各自 Vue 页面中。
export interface TravelRequest {
  destination: string;
  start_date: string;
  end_date: string;
  travelers: number;
  budget: number;
  pace: "轻松" | "适中" | "紧凑";
  hotel_level: "经济" | "舒适" | "品质";
  transport_mode: "auto" | "walk" | "driving" | "transit" | "realtime_driving";
  preferences: string[];
  dietary_preferences: string[];
  notes: string;
}

export interface PlanningRequest extends TravelRequest {
  origin: string;
}

export interface DataSource {
  provider: "open_meteo" | "amap" | "local_estimate" | "osm" | "unknown";
  freshness: "static" | "forecast" | "realtime" | "estimated";
  fetched_at?: string | null;
}

export interface Coordinate {
  latitude: number;
  longitude: number;
}

export interface WeatherDay {
  date: string;
  day_weather: string | null;
  night_weather: string | null;
  day_temperature: string | null;
  night_temperature: string | null;
  warning: string | null;
  source?: DataSource | null;
}

export interface Poi {
  id: string;
  name: string;
  address: string;
  category: "attraction" | "restaurant" | "hotel";
  latitude: number;
  longitude: number;
  type_name: string;
  image_url: string | null;
}

export interface CandidateCatalog {
  attractions: Poi[];
  restaurants: Poi[];
  hotels: Poi[];
}

export interface SpotPlan {
  poi_id: string;
  name: string;
  address: string;
  latitude: number;
  longitude: number;
  start_time: string;
  duration_minutes: number;
  note: string;
  image_url: string | null;
}

export interface MealPlan {
  poi_id: string;
  name: string;
  address: string;
  latitude: number;
  longitude: number;
  meal_type: string;
  image_url: string | null;
}

export interface HotelPlan {
  poi_id: string;
  name: string;
  address: string;
  latitude: number;
  longitude: number;
  level: string;
  image_url: string | null;
}

export interface RouteSegment {
  from_poi_id: string;
  to_poi_id: string;
  distance_km: number;
  duration_minutes: number;
  mode: string;
  polyline: Coordinate[];
  source?: DataSource | null;
  transit_lines?: TransitLine[];
  via_poi_ids?: string[];
}

export interface TransitLine {
  name: string;
  type: string;
  departure_stop: string;
  arrival_stop: string;
  via_stops: string[];
}

export interface DayPlan {
  day_index: number;
  date: string;
  theme: string;
  activities: SpotPlan[];
  meals: MealPlan[];
  hotel: HotelPlan | null;
  routes: RouteSegment[];
  weather: WeatherDay;
  budget: BudgetBreakdown | null;
  notes: string[];
}

export interface BudgetBreakdown {
  transport: number;
  local_transport?: number | null;
  intercity_transport?: number | null;
  hotel: number;
  meals: number;
  tickets: number;
  other: number;
  total: number;
}

export interface RailSeat {
  name: string;
  availability: string;
  price: number | null;
}

export interface RailSegment {
  train_code: string;
  from_station: string;
  to_station: string;
  departure_time: string;
  arrival_time: string;
  duration_minutes: number;
  seats: RailSeat[];
}

export type RailDirection = "outbound" | "return";

export interface RailOption {
  option_id: string;
  direction: RailDirection;
  travel_date: string;
  train_code: string;
  train_type: string;
  from_station: string;
  to_station: string;
  departure_time: string;
  arrival_time: string;
  duration_minutes: number;
  seats: RailSeat[];
  price_from: number | null;
  has_ticket: boolean;
  is_transfer: boolean;
  transfer_station: string | null;
  segments: RailSegment[];
  booking_url: string;
}

export interface RailChoice {
  option_id: string;
  seat_type?: string | null;
}

export interface HotelOption {
  hotel_id: string;
  name: string;
  address: string;
  latitude: number | null;
  longitude: number | null;
  star_rating: number | null;
  price_per_night: number | null;
  total_price: number | null;
  distance_km: number | null;
  image_url: string | null;
  facilities: string[];
  source: "rollinggo" | "dida" | "osm";
  booking_url: string | null;
}

export interface HotelRoom {
  room_id: string;
  name: string;
  price: number | null;
  breakfast: string | null;
  cancellation: string | null;
  available: boolean;
}

export interface HotelDetail {
  hotel_id: string;
  name: string;
  address: string;
  description: string;
  facilities: string[];
  images: string[];
  rooms: HotelRoom[];
  booking_url: string | null;
  source: "rollinggo" | "dida" | "osm";
}

export interface PlanningSelection {
  outbound: RailChoice | null;
  return_trip: RailChoice | null;
  hotel_id: string | null;
  self_arranged_outbound: boolean;
  self_arranged_return: boolean;
  self_arranged_hotel: boolean;
}

export interface IntercityPlan {
  outbound: RailOption | null;
  return_trip: RailOption | null;
  self_arranged_outbound: boolean;
  self_arranged_return: boolean;
}

export interface AccommodationPlan {
  hotel: HotelOption | null;
  check_in: string;
  check_out: string;
  nights: number;
  self_arranged: boolean;
}

export type PlanningStatus =
  | "searching"
  | "awaiting_selection"
  | "generating"
  | "completed"
  | "failed"
  | "cancelled";

export type StepStatus = "pending" | "running" | "completed" | "degraded" | "failed" | "cancelled";

export interface PlanningStep {
  name: string;
  label: string;
  status: StepStatus;
  attempts: number;
  duration_ms: number | null;
  cache_hit: boolean;
  message: string | null;
  error_code: string | null;
}

export interface PlanningSession {
  session_id: string;
  status: PlanningStatus;
  request: PlanningRequest;
  steps: PlanningStep[];
  outbound_options: RailOption[];
  return_options: RailOption[];
  outbound_transfers: RailOption[];
  return_transfers: RailOption[];
  hotel_options: HotelOption[];
  weather: WeatherDay[];
  candidates: CandidateCatalog | null;
  selection: PlanningSelection;
  trip_id: string | null;
  warnings: string[];
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface Itinerary {
  trip_id: string;
  planning_session_id?: string | null;
  revision?: number;
  destination: string;
  start_date: string;
  end_date: string;
  travelers: number;
  summary: string;
  days: DayPlan[];
  budget: BudgetBreakdown;
  intercity?: IntercityPlan | null;
  accommodation?: AccommodationPlan | null;
  tips: string[];
  warnings: string[];
  created_at: string;
}

export interface TripSummary {
  trip_id: string;
  destination: string;
  start_date: string;
  end_date: string;
  summary: string;
  created_at: string;
}

export interface DayActivityEdit {
  poi_id: string;
  start_time: string;
  duration_minutes: number;
}

export interface DayEditRequest {
  expected_revision: number;
  activities: DayActivityEdit[];
}

export interface TripAlternatives {
  trip_id: string;
  revision: number;
  attractions: Poi[];
}

export interface ApiError {
  error?: { code?: string; message?: string };
}

export type AssistantFlow = "destination_discovery" | "trip_planning";
export type AssistantSkillId = AssistantFlow;
export type MemorySlotName =
  | "origin"
  | "preferences"
  | "dietary_preferences"
  | "pace"
  | "hotel_level"
  | "transport_mode";
export type AssistantStatus =
  | "collecting"
  | "recommendation_ready"
  | "planning_started"
  | "closed";

export interface TravelDialogueSlots {
  origin: string | null;
  destination_region: string | null;
  destination_city: string | null;
  start_date: string | null;
  end_date: string | null;
  days: number | null;
  budget: number | null;
  travelers: number;
  preferences: string[];
  dietary_preferences: string[];
  distance_preference: "near" | "far" | null;
  pace: "轻松" | "适中" | "紧凑";
  hotel_level: "经济" | "舒适" | "品质";
  transport_mode: TravelRequest["transport_mode"];
  notes: string;
}

export interface SlotMetadata {
  source: "user_explicit" | "deterministic" | "memory" | "default";
  updated_turn: number;
}

export interface AssistantSkillView {
  id: AssistantSkillId;
  title: string;
  description: string;
  required_slots: string[];
  effect: "collect_requirements" | "start_planning";
}

export interface AssistantTokenUsage {
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  total_tokens: number;
}

export interface TravelMemory {
  key: MemorySlotName;
  value: string | string[];
  version: number;
  source_session_id: string;
  created_at: string;
  updated_at: string;
}

export interface TravelDialogueState {
  session_id: string;
  revision: number;
  status: AssistantStatus;
  active_flow: AssistantFlow | null;
  slots: TravelDialogueSlots;
  slot_metadata: Record<string, SlotMetadata>;
  pending_slots: string[];
  last_question: string | null;
  planning_session_id: string | null;
  token_usage: AssistantTokenUsage;
  created_at: string;
  updated_at: string;
}

export interface AssistantConversationTurn {
  sequence: number;
  user_content: string;
  assistant_content: string;
  created_at: string;
}

export interface AssistantMessageRequest {
  message_id: string;
  content: string;
}

export interface AssistantTurnResponse {
  message_id: string;
  reply: string;
  state: TravelDialogueState;
  missing_slots: string[];
  planning_session_id: string | null;
  skill: AssistantSkillView | null;
  command_source: "fast_parser" | "intent_cache" | "llm";
  context_tokens: number;
}

export interface AssistantSessionView {
  state: TravelDialogueState;
  turns: AssistantConversationTurn[];
  skill: AssistantSkillView | null;
  memories: TravelMemory[];
}
