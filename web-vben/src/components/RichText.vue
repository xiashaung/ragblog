<script setup lang="ts">
/**
 * 富文本展示组件 —— 把 AI 回答文本经 `renderRichText` 转成安全 HTML 后渲染，
 * 支持 Markdown 图片内联显示、链接、粗体、行内代码。纯展示，无副作用。
 */
import { computed } from 'vue';
import { renderRichText } from '@/utils/rich';

const props = defineProps<{ text: string }>();
const html = computed(() => renderRichText(props.text));
</script>

<template>
  <div class="rich-text" v-html="html"></div>
</template>

<style scoped>
.rich-text {
  white-space: normal;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.7;
}
.rich-text :deep(.rich-p) { margin: 0 0 8px; }
.rich-text :deep(.rich-p:last-child) { margin-bottom: 0; }
.rich-text :deep(.rich-h) {
  margin: 12px 0 6px;
  font-weight: 600;
  color: var(--app-fg);
  line-height: 1.5;
}
.rich-text :deep(.rich-h:first-child) { margin-top: 0; }
.rich-text :deep(.rich-h3) { font-size: 16px; }
.rich-text :deep(.rich-h4) { font-size: 15px; }
.rich-text :deep(.rich-h5),
.rich-text :deep(.rich-h6) { font-size: 14px; }
.rich-text :deep(.rich-ul),
.rich-text :deep(.rich-ol) {
  margin: 4px 0 8px;
  padding-left: 22px;
}
.rich-text :deep(.rich-li) { margin: 2px 0; }
.rich-text :deep(li) { margin: 2px 0; }
.rich-text :deep(.rich-img) {
  display: block;
  max-width: 100%;
  max-height: 360px;
  border-radius: 8px;
  margin: 8px 0;
  border: 1px solid var(--app-border);
}
.rich-text :deep(.rich-link) {
  color: #2563eb;
  text-decoration: none;
  word-break: break-all;
}
.rich-text :deep(.rich-link:hover) { text-decoration: underline; }
.rich-text :deep(.rich-code) {
  background: var(--app-panel-2);
  border-radius: 4px;
  padding: 1px 5px;
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 13px;
}
</style>
