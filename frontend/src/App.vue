<!-- 应用外壳只提供全局导航和页面出口，不持有旅行业务状态。 -->
<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <RouterLink class="brand" to="/">
        <span class="brand-mark">ZL</span>
        <span>
          <strong>OpenZLTravel</strong>
          <small>让每一段出发都有计划</small>
        </span>
      </RouterLink>
      <nav class="nav-links">
        <RouterLink to="/">规划行程</RouterLink>
        <RouterLink to="/history">历史行程</RouterLink>
      </nav>
    </header>
    <main class="page-content"><RouterView /></main>
  </div>
</template>
