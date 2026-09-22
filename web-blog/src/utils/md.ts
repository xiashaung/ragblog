import MarkdownIt from 'markdown-it';

/**
 * 正文渲染器：博客存的就是 Markdown 源码（与知识库同一约定），这里只做展示。
 * html:false —— 不信任正文里的原始 HTML，防 XSS；linkify 让裸链接可点。
 */
export const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: false,
  typographer: false,
});

/** 渲染为 HTML（调用方以 v-html 插入；markdown-it 已转义源码里的原始标签） */
export function renderMarkdown(text: string): string {
  return md.render(text || '');
}
