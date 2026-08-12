<!-- 历史页：列出本地行程，并提供打开与删除操作。 -->
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { deleteTrip, errorMessage, listTrips } from "../api";
import type { TripSummary } from "../types";

const router = useRouter();
const trips = ref<TripSummary[]>([]);
const error = ref("");
const loading = ref(true);

onMounted(load);

async function load(): Promise<void> {
  try {
    trips.value = await listTrips();
  } catch (requestError) {
    error.value = errorMessage(requestError);
  } finally {
    loading.value = false;
  }
}

async function remove(tripId: string): Promise<void> {
  if (!window.confirm("确定删除这份行程吗？")) return;
  try {
    await deleteTrip(tripId);
    trips.value = trips.value.filter((trip) => trip.trip_id !== tripId);
  } catch (requestError) {
    error.value = errorMessage(requestError);
  }
}
</script>

<template>
  <section class="history-page"><div class="result-header"><div><p class="eyebrow">YOUR ARCHIVE</p><h1>历史行程</h1><p class="lead">每次成功生成的计划都会自动保存在本地。</p></div><button class="primary-button compact" @click="router.push('/')">+ 新建行程</button></div>
    <div v-if="loading" class="state-card">正在读取历史行程…</div>
    <div v-else-if="error" class="state-card"><p class="error-message">{{ error }}</p></div>
    <div v-else-if="!trips.length" class="empty-card"><span class="empty-icon">◎</span><h2>还没有保存的行程</h2><p>从一次短途出发，建立你的旅行记录。</p><button class="secondary-button" @click="router.push('/')">开始规划</button></div>
    <div v-else class="history-grid"><article v-for="trip in trips" :key="trip.trip_id" class="card history-card"><div><span class="day-number">{{ trip.start_date }} — {{ trip.end_date }}</span><h2>{{ trip.destination }}</h2><p>{{ trip.summary }}</p></div><div class="history-actions"><button class="secondary-button" @click="router.push(`/result/${trip.trip_id}`)">查看</button><button class="text-button danger" @click="remove(trip.trip_id)">删除</button></div></article></div>
  </section>
</template>
