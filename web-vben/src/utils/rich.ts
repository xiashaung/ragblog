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
const ITALIC_RE = /(^|[^*])\*([^*\n]+)\*(?!\*)/g;
const DEL_RE = /~~([^~]+)~~/g;
const CODE_RE = /`([^`]+)`/g;

/** 行首块级语法（一次编译好，别在渲染循环里重复造 RegExp）。 */
const FENCE_RE = /^\s*```\s*([\w+#.-]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
/** 行已整体转义，所以 `>` 在到这里时已经是 `&gt;`（嵌套引用就退化成普通文字）。 */
const QUOTE_RE = /^&gt;\s?(.*)$/;
const HR_RE = /^(-{3,}|\*{3,}|_{3,})$/;
const UL_RE = /^([-*+])\s+(.*)$/;
const OL_RE = /^(\d+)[.)]\s+(.*)$/;
const TASK_RE = /^\[([ xX])\]\s+(.*)$/;
const TABLE_ROW_RE = /^\|.*\|$/;
const TABLE_SEP_RE = /^\|[\s:|-]+\|$/;

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

/** 行内元素渲染：图片 / 链接 / 粗体 / 斜体 / 删除线 / 行内代码（输入需已整体转义）。 */
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
  html = html.replace(DEL_RE, '<del class="rich-del">$1</del>');
  html = html.replace(BOLD_RE, '<strong>$1</strong>');
  html = html.replace(ITALIC_RE, '$1<em>$2</em>');
  html = html.replace(CODE_RE, '<code class="rich-code">$1</code>');
  return html;
}

/** 表格一行的单元格（入参需已整体转义；竖线在单元格里已被转义成字面量）。 */
function splitRow(line: string): string[] {
  return line.slice(1, -1).split('|').map((cell) => cell.trim());
}

/** 管道表 -> HTML 表格；首行是表头（分隔行的存在就是表头的信号）。 */
function renderTable(rows: string[][]): string {
  const [head, ...body] = rows;
  const cells = (row: string[], tag: string, cls: string) =>
    `<tr>${row.map((cell) => `<${tag} class="${cls}">${renderInline(cell)}</${tag}>`).join('')}</tr>`;
  const parts = [`<thead>${cells(head, 'th', 'rich-th')}</thead>`];
  if (body.length) parts.push(`<tbody>${body.map((row) => cells(row, 'td', 'rich-td')).join('')}</tbody>`);
  return `<table class="rich-table">${parts.join('')}</table>`;
}

/**
 * 把一段 Markdown / 纯文本渲染为安全 HTML。
 *
 * 块级语法：`#` 标题、`-`/`*` 与 `1.` 列表（两个空格一级嵌套，含 `- [ ]` 任务项）、
 * `>` 引用、` ``` ` 围栏代码块、`---` 分割线、`|a|b|` 管道表；行内语法见 renderInline。
 * 安全策略不变：先整体转义、再只对白名单协议放行，所以代码块里的尖括号也只会当文字显示。
 */
export function renderRichText(text: string): string {
  if (!text) return '';
  const lines = escapeHtml(text).split('\n');
  const out: string[] = [];
  let para: string[] = [];
  let listStack: { type: 'ul' | 'ol' }[] = [];
  let quote: string[] | null = null;
  let fence: { lang: string; body: string[] } | null = null;
  // 表格要先看到分隔行才敢认定首行是表头，所以整段 | 行都得缓冲，不能边读边吐
  let table: { lines: string[]; head: boolean } | null = null;

  const flushPara = () => {
    if (!para.length) return;
    out.push(`<p class="rich-p">${para.map(renderInline).join('<br>')}</p>`);
    para = [];
  };
  const closeLists = () => {
    while (listStack.length) out.push(`</${listStack.pop()!.type}>`);
  };
  const flushQuote = () => {
    if (!quote) return;
    out.push(
      `<blockquote class="rich-quote">${quote.map(renderInline).join('<br>')}</blockquote>`,
    );
    quote = null;
  };
  const flushTable = () => {
    if (!table) return;
    if (table.head) out.push(renderTable(table.lines.map(splitRow)));
    else para.push(...table.lines); // 含竖线的普通文本，不是表
    table = null;
  };
  /** 关掉所有开着的块：只有一行都不属于它时才需要（空行、围栏、标题）。 */
  const breakBlocks = () => {
    flushTable();
    flushPara();
    closeLists();
    flushQuote();
  };
  /** 对齐列表层级：不够就补开、超了就关掉（同一层换了列表类型也重建）。 */
  const syncList = (type: 'ul' | 'ol', level: number) => {
    while (listStack.length > level + 1) out.push(`</${listStack.pop()!.type}>`);
    if (listStack.length === level + 1 && listStack[level].type !== type) {
      out.push(`</${listStack.pop()!.type}>`);
    }
    while (listStack.length < level + 1) {
      out.push(`<${type} class="rich-${type}">`);
      listStack.push({ type });
    }
  };
  const emitPre = (block: { lang: string; body: string[] }) => {
    // 语言名挂在 data-lang 上由 CSS 出角标；body 已整体转义，这里不再加工
    const attr = block.lang ? ` data-lang="${block.lang}"` : '';
    out.push(`<pre class="rich-pre"${attr}><code>${block.body.join('\n')}</code></pre>`);
  };

  for (const rawLine of lines) {
    // 围栏内的行原样收集：不缩进折叠、不解析语法，直到碰到闭合围栏
    if (fence) {
      const closing = FENCE_RE.exec(rawLine);
      if (closing && !closing[1]) {
        emitPre(fence);
        fence = null;
      } else {
        fence.body.push(rawLine);
      }
      continue;
    }

    const line = rawLine.trim();
    if (TABLE_ROW_RE.test(line)) {
      flushPara();
      closeLists();
      flushQuote();
      if (!table) table = { lines: [], head: false };
      // 分隔行只能出现在表头下面一行；它只是声明表头，自身不是数据
      if (table.lines.length === 1 && TABLE_SEP_RE.test(line)) {
        table.head = true;
        continue;
      }
      table.lines.push(line);
      continue;
    }
    flushTable();

    if (!line) {
      breakBlocks(); // 空行是所有块的边界（也顺带让缓冲中的 | 行定型）
      continue;
    }

    const fenceOpen = FENCE_RE.exec(line);
    if (fenceOpen) {
      breakBlocks();
      fence = { lang: fenceOpen[1], body: [] };
      continue;
    }

    let m;
    if ((m = HEADING_RE.exec(line))) {
      breakBlocks();
      // 对话气泡与文档抽屉共用这一份渲染，标题一律降级一档（# -> h3）避免字号过大。
      const level = Math.min(m[1].length + 2, 6);
      out.push(`<h${level} class="rich-h">${renderInline(m[2])}</h${level}>`);
      continue;
    }
    if (HR_RE.test(line)) {
      breakBlocks();
      out.push('<hr class="rich-hr">');
      continue;
    }
    if ((m = QUOTE_RE.exec(line))) {
      flushPara();
      closeLists();
      (quote ??= []).push(m[1]);
      continue;
    }

    let item: { type: 'ul' | 'ol'; body: string } | null = null;
    if ((m = UL_RE.exec(line))) item = { type: 'ul', body: m[2] };
    else if ((m = OL_RE.exec(line))) item = { type: 'ol', body: m[2] };
    if (item) {
      flushPara();
      flushQuote();
      const indent = rawLine.length - rawLine.trimStart().length;
      syncList(item.type, Math.min(Math.floor(indent / 2), 3));
      const task = TASK_RE.exec(item.body);
      // 不写 `</li>`：嵌套列表靠浏览器的自动闭合才能落进父项里（否则成了兄弟节点）
      out.push(
        task
          ? `<li class="rich-task"><span class="rich-task-box">${
              task[1] === ' ' ? '☐' : '☑'
            }</span>${renderInline(task[2])}`
          : `<li>${renderInline(item.body)}`,
      );
      continue;
    }

    closeLists();
    flushQuote();
    para.push(line);
  }

  if (fence) emitPre(fence); // 围栏没闭合：至少把已经写的东西展示出来，不吞输入
  breakBlocks();
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
