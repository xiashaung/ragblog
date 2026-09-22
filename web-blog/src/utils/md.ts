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

/** 目录项：id 对应正文里加过锚点的标题 */
export interface TocItem {
  id: string;
  text: string;
  level: number;
}

/**
 * 给渲染结果里的 h1–h3 补顺序 id，并抽出目录数据。
 * 只做正则后处理而不改 markdown-it 规则：html:false 下标题内只可能有
 * 行内标签，剔除标签后的纯文本就是安全的大纲文字。
 */
export function attachHeadingAnchors(html: string): { html: string; toc: TocItem[] } {
  const toc: TocItem[] = [];
  let seq = 0;
  const out = html.replace(/<h([1-3])>([\s\S]*?)<\/h\1>/g, (_m, level: string, inner: string) => {
    seq += 1;
    const id = `heading-${seq}`;
    const text = inner.replace(/<[^>]+>/g, '').trim();
    toc.push({ id, text, level: Number(level) });
    return `<h${level} id="${id}">${inner}</h${level}>`;
  });
  return { html: out, toc };
}
