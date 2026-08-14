// 页面只调用稳定业务函数，不直接处理 Axios 响应结构。
import axios from "axios";

import type {
  AssistantMessageRequest,
  AssistantSkillView,
  AssistantSessionView,
  AssistantTurnResponse,
  DayEditRequest,
  HotelDetail,
  MemorySlotName,
  Itinerary,
  PlanningRequest,
  PlanningSelection,
  PlanningSession,
  RailDirection,
  RailOption,
  TravelRequest,
  TravelMemory,
  TripAlternatives,
  TripSummary,
} from "./types";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000",
  headers: { "Content-Type": "application/json" },
  timeout: Number(import.meta.env.VITE_API_TIMEOUT_MS || 20000),
});

export async function createAssistantSession(): Promise<AssistantSessionView> {
  const response = await api.post<AssistantSessionView>("/api/assistant-sessions");
  return response.data;
}

export async function getAssistantSession(
  sessionId: string,
): Promise<AssistantSessionView> {
  const response = await api.get<AssistantSessionView>(
    `/api/assistant-sessions/${sessionId}`,
  );
  return response.data;
}

export async function sendAssistantMessage(
  sessionId: string,
  message: AssistantMessageRequest,
): Promise<AssistantTurnResponse> {
  const response = await api.post<AssistantTurnResponse>(
    `/api/assistant-sessions/${sessionId}/messages`,
    message,
  );
  return response.data;
}

export async function listAssistantSkills(): Promise<AssistantSkillView[]> {
  const response = await api.get<AssistantSkillView[]>("/api/assistant-skills");
  return response.data;
}

export async function listAssistantMemories(): Promise<TravelMemory[]> {
  const response = await api.get<TravelMemory[]>("/api/assistant-memories");
  return response.data;
}

export async function deleteAssistantMemory(key: MemorySlotName): Promise<void> {
  await api.delete(`/api/assistant-memories/${key}`);
}

export async function createPlanningSession(
  request: PlanningRequest,
  idempotencyKey: string,
): Promise<PlanningSession> {
  const response = await api.post<PlanningSession>("/api/planning-sessions", request, {
    headers: { "Idempotency-Key": idempotencyKey },
  });
  return response.data;
}

export async function getPlanningSession(sessionId: string): Promise<PlanningSession> {
  const response = await api.get<PlanningSession>(`/api/planning-sessions/${sessionId}`);
  return response.data;
}

export async function getReadiness(): Promise<{ hotel_provider: string }> {
  const response = await api.get<{ hotel_provider: string }>("/ready");
  return response.data;
}

export async function updatePlanningSelection(
  sessionId: string,
  selection: PlanningSelection,
): Promise<PlanningSession> {
  const response = await api.put<PlanningSession>(
    `/api/planning-sessions/${sessionId}/selection`,
    selection,
  );
  return response.data;
}

export async function loadRailTransfers(
  sessionId: string,
  direction: RailDirection,
): Promise<RailOption[]> {
  const response = await api.post<RailOption[]>(
    `/api/planning-sessions/${sessionId}/rail/transfers`,
    { direction },
  );
  return response.data;
}

export async function getHotelDetail(
  sessionId: string,
  hotelId: string,
): Promise<HotelDetail> {
  const response = await api.get<HotelDetail>(
    `/api/planning-sessions/${sessionId}/hotels/${hotelId}`,
  );
  return response.data;
}

export async function generatePlanningSession(sessionId: string): Promise<PlanningSession> {
  const response = await api.post<PlanningSession>(
    `/api/planning-sessions/${sessionId}/generate`,
  );
  return response.data;
}

export async function retryPlanningSession(sessionId: string): Promise<PlanningSession> {
  const response = await api.post<PlanningSession>(
    `/api/planning-sessions/${sessionId}/retry`,
  );
  return response.data;
}

export async function cancelPlanningSession(sessionId: string): Promise<void> {
  await api.delete(`/api/planning-sessions/${sessionId}`);
}

export async function createTrip(request: TravelRequest): Promise<Itinerary> {
  const response = await api.post<Itinerary>("/api/trips", request, { timeout: 180000 });
  return response.data;
}

export async function listTrips(): Promise<TripSummary[]> {
  const response = await api.get<TripSummary[]>("/api/trips");
  return response.data;
}

export async function getTrip(tripId: string): Promise<Itinerary> {
  const response = await api.get<Itinerary>(`/api/trips/${tripId}`);
  return response.data;
}

export async function getTripAlternatives(tripId: string): Promise<TripAlternatives> {
  const response = await api.get<TripAlternatives>(`/api/trips/${tripId}/alternatives`);
  return response.data;
}

export async function editTripDay(
  tripId: string,
  dayIndex: number,
  edit: DayEditRequest,
): Promise<Itinerary> {
  const response = await api.patch<Itinerary>(`/api/trips/${tripId}/days/${dayIndex}`, edit);
  return response.data;
}

export async function deleteTrip(tripId: string): Promise<void> {
  await api.delete(`/api/trips/${tripId}`);
}

export function markdownUrl(tripId: string): string {
  const base = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
  return `${base}/api/trips/${tripId}/export/markdown`;
}

export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    if (error.code === "ECONNABORTED" || error.code === "ETIMEDOUT") {
      return "请求超时，任务状态已保留，请稍后重试。";
    }
    const data = error.response?.data as { error?: { message?: string } } | undefined;
    return data?.error?.message || "请求失败，请检查后端服务是否启动。";
  }
  return "操作失败，请稍后重试。";
}
