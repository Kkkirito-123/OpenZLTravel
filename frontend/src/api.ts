// 统一封装后端 HTTP 接口与错误提示，页面不直接处理 Axios 响应结构。
import axios from "axios";

import type { Itinerary, TravelRequest, TripSummary } from "./types";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000",
  headers: { "Content-Type": "application/json" },
});

export async function createTrip(request: TravelRequest): Promise<Itinerary> {
  const response = await api.post<Itinerary>("/api/trips", request);
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

export async function deleteTrip(tripId: string): Promise<void> {
  await api.delete(`/api/trips/${tripId}`);
}

export function markdownUrl(tripId: string): string {
  const base = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
  return `${base}/api/trips/${tripId}/export/markdown`;
}

export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const message = (error.response?.data as { error?: { message?: string } } | undefined)?.error?.message;
    return message || "请求失败，请检查后端服务是否启动。";
  }
  return "操作失败，请稍后重试。";
}
