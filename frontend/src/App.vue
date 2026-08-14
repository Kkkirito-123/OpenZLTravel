<!-- 应用外壳只提供导航和页面出口，不持有旅行业务状态。 -->
<script setup lang="ts">
import { Clock3, MessageCircle, Plus, Route } from "@lucide/vue";
import { RouterLink, RouterView } from "vue-router";
</script>

<template>
  <div class="app-shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <header class="topbar">
      <RouterLink class="brand" to="/" aria-label="OpenZLTravel 首页">
        <span class="brand-mark"><Route :size="20" /></span>
        <span><strong>OpenZLTravel</strong><small>综合旅行工作台</small></span>
      </RouterLink>
      <nav class="nav-links" aria-label="主导航">
        <RouterLink to="/"><MessageCircle :size="17" />旅行助手</RouterLink>
        <RouterLink to="/plan"><Plus :size="17" />表单规划</RouterLink>
        <RouterLink to="/history"><Clock3 :size="17" />历史行程</RouterLink>
      </nav>
    </header>
    <main id="main-content" class="page-content"><RouterView /></main>
  </div>
</template>
