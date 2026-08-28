export type PlanningPhase = "planning" | "awaiting_route_confirmation" | "completed";

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
  requested_places?: string[];
  preferences?: string[];
  dietary_preferences?: string[];
}

export interface CityFact {
  name: string;
  adcode?: string | null;
  latitude?: number | null;
  longitude?: number | null;
}

export interface Poi {
  id: string;
  name: string;
  address: string;
  category: "attraction" | "restaurant" | "hotel";
  latitude: number;
  longitude: number;
  image_url?: string | null;
  type_name?: string;
  tags?: string[];
}

export interface CandidateCatalog {
  attractions: Poi[];
  restaurants: Poi[];
  hotels: Poi[];
  required_attraction_ids?: string[];
}

export interface DestinationCandidate {
  candidate_id: string;
  city: CityFact;
  score: number;
  reasons: string[];
  attraction_count?: number;
  restaurant_count?: number;
  hotel_count?: number;
}

export interface RailSeat {
  name: string;
  availability?: string;
  price?: number | null;
}

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
  booking_url?: string;
}

export interface HotelOption {
  hotel_id: string;
  name: string;
  address?: string;
  price_per_night?: number | null;
  total_price?: number | null;
  image_url?: string | null;
  star_rating?: number | null;
  facilities?: string[];
  source?: string;
  latitude?: number | null;
  longitude?: number | null;
}

export interface WeatherDay {
  date: string;
  day_weather?: string | null;
  night_weather?: string | null;
  day_temperature?: string | null;
  night_temperature?: string | null;
  warning?: string | null;
  source?: string | null;
}

export interface RouteSegment {
  from_poi_id: string;
  to_poi_id: string;
  distance_km: number;
  duration_minutes: number;
  mode: string;
  cost?: number | null;
  polyline?: Array<[number, number]>;
  source?: string;
}

export interface RailChoice {
  option_id: string;
  seat_type?: string | null;
}

export interface TravelSelection {
  attraction_ids: string[];
  outbound?: RailChoice | null;
  return_trip?: RailChoice | null;
  hotel_id?: string | null;
  self_arranged_outbound?: boolean;
  self_arranged_return?: boolean;
  self_arranged_hotel?: boolean;
}

export interface TravelFacts {
  city?: CityFact | null;
  catalog?: CandidateCatalog | null;
  outbound_options: RailOption[];
  return_options: RailOption[];
  hotel_options: HotelOption[];
  weather: WeatherDay[];
  routes: Record<string, RouteSegment[]>;
}

export interface ActivityDraft {
  poi_id: string;
  start_time: string;
  duration_minutes: number;
  note?: string;
}

export interface DayDraft {
  day_index: number;
  theme: string;
  activities: ActivityDraft[];
  meal_ids?: string[];
  hotel_id?: string | null;
  notes?: string[];
}

export interface ItineraryDraft {
  summary: string;
  days: DayDraft[];
  tips?: string[];
}

export interface BudgetBreakdown {
  intercity_transport?: number | null;
  local_transport?: number | null;
  hotel?: number | null;
  meals_estimated?: number;
  tickets_estimated?: number;
  total_known?: number;
  currency?: "CNY";
}

export interface PlaceSnapshot {
  fact_id: string;
  name: string;
  address: string;
  category: "attraction" | "restaurant" | "hotel";
  latitude?: number | null;
  longitude?: number | null;
  image_url?: string | null;
}

export interface TravelOrder {
  order_id: string;
  created_at: string;
  facts_refreshed_at: string;
  requirements: TravelRequirements;
  facts: TravelFacts;
  selection: TravelSelection;
  fact_metadata?: Record<string, { source: string; queried_at: string }>;
}

export interface TripRecord {
  trip_id: string;
  requirements: TravelRequirements;
  city: CityFact;
  selection: TravelSelection;
  outbound_rail?: RailOption | null;
  return_rail?: RailOption | null;
  draft: ItineraryDraft;
  weather: WeatherDay[];
  routes: Record<string, RouteSegment[]>;
  budget: BudgetBreakdown;
  place_index: Record<string, PlaceSnapshot>;
  warnings?: string[];
  created_at?: string;
}

export interface TripSummary {
  trip_id: string;
  destination: string;
  start_date?: string | null;
  end_date?: string | null;
  summary?: string;
  created_at?: string;
}

export interface GraphNotice {
  code: string;
  message: string;
  node: string;
}

export interface AssistantMessage {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface AssistantSnapshot {
  session_id: string;
  messages: AssistantMessage[];
  requirements: TravelRequirements;
  destination_candidates: DestinationCandidate[];
  facts: TravelFacts;
  selection: TravelSelection;
  fact_metadata?: Record<string, { source: string; queried_at: string }>;
  status: "collecting" | "ready" | "submitted";
}

export interface AssistantAction {
  kind: "select_destination" | "select_attractions" | "select_outbound" | "select_return" | "select_hotel" | "self_arrange";
  candidate_id?: string;
  attraction_ids?: string[];
  option_id?: string;
  seat_type?: string;
  hotel_id?: string;
  target?: "outbound" | "return" | "hotel";
}

export interface AssistantHandoff {
  order: TravelOrder;
  order_token: string;
}

export interface ToolEvent {
  name: string;
  status: "completed";
  artifact?: string;
}

export interface RoutePreviewInterrupt {
  kind: "route_preview";
  question: string;
  budget?: BudgetBreakdown | null;
  budget_limit?: number | null;
  is_over_budget?: boolean;
  error?: { code: string; message: string } | null;
}

export interface PlanningState {
  phase: PlanningPhase;
  order?: TravelOrder;
  facts: TravelFacts;
  draft?: ItineraryDraft | null;
  budget?: BudgetBreakdown | null;
  trip_id?: string | null;
  warnings: GraphNotice[];
}

export interface PlanningSnapshot {
  state: PlanningState;
  interrupt: RoutePreviewInterrupt | null;
  status: "idle" | "busy" | "interrupted" | "error";
}

export const EMPTY_FACTS: TravelFacts = {
  outbound_options: [],
  return_options: [],
  hotel_options: [],
  weather: [],
  routes: {},
};

export function emptyAssistantSnapshot(): AssistantSnapshot {
  return {
    session_id: "",
    messages: [],
    requirements: {},
    destination_candidates: [],
    facts: { ...EMPTY_FACTS },
    selection: { attraction_ids: [] },
    status: "collecting",
  };
}

export function emptyPlanningState(): PlanningState {
  return {
    phase: "planning",
    facts: { ...EMPTY_FACTS },
    warnings: [],
  };
}
