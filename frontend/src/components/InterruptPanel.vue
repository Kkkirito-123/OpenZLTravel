<script setup lang="ts">
import ClarificationCard from "./ClarificationCard.vue";
import DestinationSelection from "./DestinationSelection.vue";
import TravelSelection from "./TravelSelection.vue";
import type { ResumePayload, TravelInterrupt, TravelSelection as Selection } from "../types";

defineProps<{
  interrupt: TravelInterrupt;
  selection: Selection;
  busy: boolean;
}>();
const emit = defineEmits<{ resume: [payload: ResumePayload] }>();
</script>

<template>
  <ClarificationCard
    v-if="interrupt.kind === 'clarification'"
    :interrupt="interrupt"
    :busy="busy"
    @submit="emit('resume', { kind: 'clarification', values: $event })"
  />
  <DestinationSelection
    v-else-if="interrupt.kind === 'destination_selection'"
    :interrupt="interrupt"
    :busy="busy"
    @select="emit('resume', { kind: 'destination_selection', candidate_id: $event })"
  />
  <TravelSelection
    v-else
    :interrupt="interrupt"
    :existing="selection"
    :busy="busy"
    @select="emit('resume', { kind: 'travel_selection', selection: $event })"
  />
</template>
