<script setup>
import { computed } from 'vue'

/** 检索流水线 trace 的可折叠展示：耗时条形图 + 召回数 + 剔除数。 */
const props = defineProps({
  trace: { type: Object, default: null },
})

const timings = computed(() => {
  if (!props.trace?.timings_ms) return []
  const entries = Object.entries(props.trace.timings_ms).map(([k, v]) => ({
    label: k.replace(/^\d_/, '').replace(/_/g, ' '),
    ms: v,
  }))
  const max = Math.max(...entries.map(e => e.ms), 1)
  return entries.map(e => ({ ...e, pct: Math.max((e.ms / max) * 100, 1) }))
})
</script>

<template>
  <details class="trace-panel" v-if="trace">
    <summary>🔍 检索链路 Trace（耗时 / 召回 / 复核）</summary>
    <div class="trace-body">
      <div class="trace-slots" v-if="trace.slots">
        <span class="trace-label">解析槽位：</span>
        <code>{{ trace.slots }}</code>
      </div>
      <div class="trace-recall" v-if="trace.recall_counts && Object.keys(trace.recall_counts).length">
        <span class="trace-label">三路召回：</span>
        <span v-for="(v, k) in trace.recall_counts" :key="k" class="recall-chip">{{ k }} = {{ v }}</span>
        <span class="recall-chip">复核剔除 = {{ trace.rejected_count }}</span>
      </div>
      <div class="trace-timings" v-if="timings.length">
        <div class="trace-label" style="margin-bottom: 8px">各阶段耗时（ms）：</div>
        <div v-for="t in timings" :key="t.label" class="timing-row">
          <span class="timing-label">{{ t.label }}</span>
          <div class="timing-bar-wrap">
            <div class="timing-bar" :style="{ width: t.pct + '%' }"></div>
          </div>
          <span class="timing-value">{{ t.ms }}</span>
        </div>
      </div>
    </div>
  </details>
</template>
