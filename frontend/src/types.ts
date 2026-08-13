// 与后端响应一一对应的前端类型，仅描述数据结构，不承载页面状态。
export interface TravelRequest {
  destination: string;
  start_date: string;
  end_date: string;
  travelers: number;
  budget: number;
  pace: "轻松" | "适中" | "紧凑";
  hotel_level: "经济" | "舒适" | "品质";
  preferences: string[];
  dietary_preferences: string[];
  notes: string;
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
  hotel: number;
  meals: number;
  tickets: number;
  other: number;
  total: number;
}

export interface Itinerary {
  trip_id: string;
  destination: string;
  start_date: string;
  end_date: string;
  travelers: number;
  summary: string;
  days: DayPlan[];
  budget: BudgetBreakdown;
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

export interface ApiError {
  error?: { code?: string; message?: string };
}
