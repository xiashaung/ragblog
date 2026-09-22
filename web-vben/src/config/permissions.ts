/**
 * 权限码的中文名 —— 只用来给「被禁用的按钮」拼悬停提示。
 *
 * 唯一事实源是后端 app/rbac.py::PERMISSION_CATALOG（角色页勾选项、403 文案都读它）。
 * 这里只是同一份名字的前端副本，专门服务一个场景：无权时按钮要告诉用户缺哪一条，
 * 而此时往往还拿不到目录接口（/admin/permissions 本身要 role:read）。
 * 两边对不上时不会报错，提示会退回显示裸 code。
 */
export const PERMISSION_LABELS: Record<string, string> = {
  'account:read': '查看账号',
  'account:manage': '管理账号',
  'role:read': '查看角色',
  'role:manage': '管理角色',
  'session:read': '查看会话',
  'session:revoke': '吊销会话',
  'analytics:read': '智能问数',
  'datasource:read': '查看数据源',
  'datasource:manage': '管理数据源',
  'system:read': '查看模型配置',
  'system:manage': '管理模型配置',
  'knowledge:read': '查看知识库',
  'knowledge:manage': '管理知识库',
  'blog:read': '查看博客',
  'blog:manage': '管理博客',
};

/** 把权限码翻成「中文名（code）」，查不到名字就只给 code。 */
export function permLabel(code: string): string {
  const label = PERMISSION_LABELS[code];
  return label ? `${label}（${code}）` : code;
}

/**
 * 无权提示文案。
 *
 * 口径与后端 403 保持一致（都是「权限不足，需要：…」），差别只在按钮上要多说一句
 * 「已禁用」，让用户知道这不是功能坏了。
 *
 * @param codes 需要的权限码
 * @param anyMode true = 任一命中即可；false = 要求全部命中
 */
export function permTip(codes: string[], anyMode = false): string {
  return `无操作权限，已禁用。${permNeed(codes, anyMode)}`;
}

/** 只说「需要哪条权限」，不带「已禁用」那半句 —— 适合 toast / 说明文字。 */
export function permNeed(codes: string[], anyMode = false): string {
  const wanted = codes.map(permLabel).join(anyMode ? ' 或 ' : '、');
  return `${anyMode ? '需要以下任一权限：' : '需要权限：'}${wanted}`;
}
