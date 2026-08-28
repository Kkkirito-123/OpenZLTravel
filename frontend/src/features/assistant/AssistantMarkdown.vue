<script setup lang="ts">
import DOMPurify from "dompurify";
import { marked } from "marked";
import { computed } from "vue";

const props = defineProps<{ content: string }>();

const html = computed(() => DOMPurify.sanitize(marked.parse(props.content) as string));
</script>

<template>
  <div class="assistant-markdown" v-html="html" />
</template>
