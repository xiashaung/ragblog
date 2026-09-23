<script setup lang="ts">
/**
 * 富文本展示组件 —— 把 AI 回答与知识文档里的 Markdown 经 `renderRichText`
 * 转成安全 HTML 后渲染（标题 / 列表 / 引用 / 代码块 / 表格 / 图片 / 链接）。
 * 纯展示，无副作用。
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
  color: var(--app-fg);
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
  color: var(--app-link);
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

/* 代码块：语言名由 CSS 从 data-lang 长成角标，不占正文流 */
.rich-text :deep(.rich-pre) {
  position: relative;
  margin: 8px 0;
  padding: 8px 10px;
  background: var(--app-panel-2);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  overflow-x: auto;
}
.rich-text :deep(.rich-pre[data-lang]) { padding-top: 22px; }
.rich-text :deep(.rich-pre[data-lang]::before) {
  content: attr(data-lang);
  position: absolute;
  top: 5px;
  left: 10px;
  font-size: 11px;
  letter-spacing: 0.4px;
  color: var(--app-muted);
  text-transform: uppercase;
}
.rich-text :deep(.rich-pre code) {
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre;
  background: none;
  padding: 0;
}

/* 表格：知识库里的对照表靠它，窄屏下横向滚动而不是压成一团 */
.rich-text :deep(.rich-table) {
  width: 100%;
  margin: 8px 0;
  border-collapse: collapse;
  font-size: 13px;
  display: block;
  overflow-x: auto;
  white-space: nowrap;
}
.rich-text :deep(.rich-th),
.rich-text :deep(.rich-td) {
  border: 1px solid var(--app-border);
  padding: 4px 8px;
  text-align: left;
}
.rich-text :deep(.rich-th) {
  background: var(--app-panel-2);
  font-weight: 600;
}

.rich-text :deep(.rich-quote) {
  margin: 6px 0;
  padding: 2px 0 2px 10px;
  border-left: 3px solid var(--app-border);
  color: var(--app-muted);
}
.rich-text :deep(.rich-hr) {
  margin: 12px 0;
  border: 0;
  border-top: 1px solid var(--app-border);
}
.rich-text :deep(.rich-del) { color: var(--app-muted); }
.rich-text :deep(.rich-task-box) { margin-right: 4px; }
</style>
