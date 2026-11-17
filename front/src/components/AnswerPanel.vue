<script setup>
/**
 * 答案展示区。三种状态：
 *   - 流式进行中：显示打字机文本 + 光标
 *   - 完成：显示完整答案 + guard 徽章
 *   - fallback 覆盖：显示模板答案 + "已自动降级"提示（透明告知用户）
 */
defineProps({
  text: { type: String, default: '' },
  mode: { type: String, default: '' },        // llm / template
  guardOk: { type: Boolean, default: false },
  guardReason: { type: String, default: '' },
  streaming: { type: Boolean, default: false },
  degraded: { type: Boolean, default: false }, // 发生过 fallback 覆盖
})
</script>

<template>
  <section class="answer-panel" v-if="text || streaming">
    <div class="answer-head">
      <span class="answer-title">🤖 推荐答案</span>
      <div class="answer-badges">
        <span v-if="streaming" class="badge badge-streaming">
          <span class="dot-pulse"></span> 生成中
        </span>
        <template v-if="!streaming && mode">
          <span class="badge" :class="mode === 'llm' ? 'badge-llm' : 'badge-template'">
            {{ mode === 'llm' ? 'LLM 生成' : '模板答案' }}
          </span>
          <span class="badge" :class="guardOk ? 'badge-pass' : 'badge-guard'">
            {{ guardOk ? '✓ 校验通过' : '⚠ ' + (guardReason || '已降级') }}
          </span>
        </template>
      </div>
    </div>
    <div class="answer-body">
      <span class="answer-text">{{ text }}</span><span v-if="streaming" class="cursor">▋</span>
    </div>
    <div v-if="degraded" class="degraded-note">
      ℹ️ 流式答案未通过引用校验，已自动切换为基于结构化数据的可靠答案。
    </div>
  </section>
</template>
