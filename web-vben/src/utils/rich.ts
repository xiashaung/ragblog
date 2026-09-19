/**
 * 轻量富文本渲染 —— 把 AI 回答里的 Markdown 图片 / 链接 / 粗体 / 行内代码
 * 转成安全的 HTML 字符串，供 `v-html` 使用。
 *
 * 安全策略：先整体转义 HTML（杜绝注入），再仅对白名单协议
 * （http/https 与 data:image/*;base64）的图片、链接放行；
 * 其余 url 一律降级为纯文本，模型即便被诱导输出 `javascript:` / `<script>` 也无法执行。
 */

const IMG_RE = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g;
const LINK_RE = /(?<!\!)\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g;
const BOLD_RE = /\*\*([^*]+)\*\*/g;
const CODE_RE = /`([^`]+)`/g;

const ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

export function escapeHtml(input: string): string {
  return input.replace(/[&<>"']/g, (c) => ESCAPE_MAP[c] ?? c);
}

/** 仅放行 http(s) 与内联 base64 图片；命中返回原样字符串，否则 null。 */
function safeUrl(raw: string): string | null {
  const url = raw.trim();
  if (/^https?:\/\//i.test(url)) return url;
  if (/^data:image\/(png|jpe?g|gif|webp|bmp);base64,[a-z0-9+/=]+$/i.test(url)) return url;
  return null;
}

/** 读取图片文件为 data URL（对话附件、上传预览共用）。 */
export function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

/** 判断是否图片文件（按 MIME 或常见扩展名）。 */
export function isImageFile(file: File): boolean {
  if (file.type.startsWith('image/')) return true;
  return /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name);
}

/** 行内元素渲染：图片 / 链接 / 粗体 / 斜体 / 行内代码（输入需已整体转义）。 */
function renderInline(escaped: string): string {
  let html = escaped;
  // 图片必须先于链接处理，否则 `![alt](url)` 会被链接规则截断。
  html = html.replace(IMG_RE, (_m, alt: string, url: string) => {
    const src = safeUrl(url);
    return src
      ? `<img class="rich-img" src="${src}" alt="${alt}" loading="lazy">`
      : alt || '';
  });
  html = html.replace(LINK_RE, (_m, label: string, url: string) => {
    const href = safeUrl(url);
    return href
      ? `<a class="rich-link" href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : label;
  });
  html = html.replace(BOLD_RE, '<strong>$1</strong>');
  html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  html = html.replace(CODE_RE, '<code class="rich-code">$1</code>');
  return html;
}

/**
 * 把一段 Markdown / 纯文本渲染为安全 HTML。
 * 除行内元素外，额外支持块级结构：`#` 标题、`-`/`*` 无序列表、`1.` 有序列表，
 * 以便 AI 整理后的结构化回答能正常呈现。安全策略仍为“先转义、后白名单”。
 */
export function renderRichText(text: string): string {
  if (!text) return '';
  const lines = escapeHtml(text).split('\n');
  const out: string[] = [];
  let para: string[] = [];
  let listType: 'ul' | 'ol' | null = null;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p class="rich-p">${para.map(renderInline).join('<br>')}</p>`);
      para = [];
    }
  };
  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      flushPara();
      closeList();
      continue;
    }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara();
      closeList();
      // 对话气泡内降级一档（# -> h3），避免字号过大。
      const level = Math.min(m[1].length + 2, 6);
      out.push(`<h${level} class="rich-h">${renderInline(m[2])}</h${level}>`);
      continue;
    }
    if ((m = line.match(/^[-*+]\s+(.*)$/))) {
      flushPara();
      if (listType !== 'ul') {
        closeList();
        out.push('<ul class="rich-ul">');
        listType = 'ul';
      }
      out.push(`<li>${renderInline(m[1])}</li>`);
      continue;
    }
    if ((m = line.match(/^\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (listType !== 'ol') {
        closeList();
        out.push('<ol class="rich-ol">');
        listType = 'ol';
      }
      out.push(`<li>${renderInline(m[1])}</li>`);
      continue;
    }
    closeList();
    para.push(line);
  }
  flushPara();
  closeList();
  return out.join('');
}

/** 从消息正文里抽取所有 Markdown 图片的 url（用于附件回显）。 */
export function extractImageUrls(text: string): string[] {
  if (!text) return [];
  const urls: string[] = [];
  for (const m of text.matchAll(IMG_RE)) {
    const src = safeUrl(m[2]);
    if (src) urls.push(src);
  }
  return urls;
}
