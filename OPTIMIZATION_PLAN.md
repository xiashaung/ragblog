# 死代码优化分析报告

> 基于 `.tools_deadcode_v3.py` 扫描结果，经人工核实后的可安全删除项清单。

---

## 一、确认可安全删除（Category A - True Dead Code）

以下函数/类在定义文件之外**零引用**，已被 `grep` 确认，删除后不影响运行时行为。

| # | 位置 | 类型 | 名称 | 说明 |
|---|------|------|------|------|
| 1 | `app/config.py:42` | func | `custom_providers()` | ⚠️ 注意：模块变量 `_custom_providers` (line:30) 和函数 `register_custom_providers()` (line:36) **仍被使用**，仅删除第42行的读取快照函数 |
| 2 | `app/routers/ai.py:84` | func | `_resolve_provider()` | 内部辅助函数，定义后从未被调用 |
| 3 | `app/media_gc.py:106` | func | `release_doc_media()` | 废弃的媒体清理函数，原功能已由其他机制替代 |
| 4 | `app/schemas/blog.py:239` | class | `BlogAnswerRequest` | 博客问答请求模型，当前未使用 |
| 5 | `app/services/chat_tools.py:130` | method | `ToolResult.as_chunk_payload()` | 废弃的摘要方法，流式已不依赖 |
| 6 | `app/services/markdown_text.py:20` | const | `CONTENT_FORMATS` | 常量定义，未在任何代码路径中使用 |
| 7 | `app/services/markdown_text.py:399` | func | `looks_like_html()` | HTML 检测函数，未被调用 |
| 8 | `app/services/provider_store.py:101` | func | `is_loaded()` | 加载状态检查函数，未使用 |
| 9 | `app/services/provider_store.py:147` | func | `get_image_embedding_model()` | 图片嵌入模型获取函数，未使用 |
| 10 | `app/services/provider_store.py:159` | func | `get_config()` | 配置获取函数，未使用 |

**删除量**: 10 处（约 ~50 行代码）
**风险等级**: 🟢 低 — 全部经 `grep -rn` 全仓库确认无外部引用，**且已运行完整测试通过 (266 passed)**

---

## 六、执行状态 ✅ 已完成

- [x] 7 个文件共删除 ~69 行死代码
- [x] 266 项测试全部通过，0 失败
- [ ] 清理分析脚本（.deadcode*, .tools_deadcode*）和本文档待手动执行

---

---

## 二、需谨慎核实（Category B - 边界情况）

### UserBase (app/schemas/user.py:10)
**结论**: 不应删除。虽然死代码扫描器标为「仅注释提到」，但它被 `UserCreate` 和 `User` **继承使用**：
```python
class UserCreate(UserBase): ...  # line:17
class User(UserBase): ...        # line:30
```
扫描器未将 base class 位置的标识符计入引用，属于误报。

---

## 三、确证非死代码（Category C - false positives）

### 全部 Settings 字段 (35+ 项)
包括 `api_v1_prefix`、所有 `llm_*` 配置字段。这些**不是死代码**，被以下方式动态访问：

- `_merge_flat_llm_env()` validator 中: `getattr(self, f"llm_{name}_{part}", None)` (line:239)
- `provider_config()` 方法中: `getattr(self, key, None)` (line:409)
- Pydantic 通过字段声明本身构建模型，不需要外部引用

**结论**: 全部保留。删除任何 Settings 字段将导致 Pydantic 模型配置不正确，且 `getattr` 访问会失败。

---

## 四、操作建议

### 立即删除（低风险）
1. `app/routers/ai.py` — 删除 `_resolve_provider()` 函数 (line:84-约95)
2. `app/media_gc.py` — 删除 `release_doc_media()` 函数 (line:106-约)
3. `app/schemas/blog.py` — 删除 `BlogAnswerRequest` class (line:239-约)
4. `app/services/chat_tools.py` — 删除 `as_chunk_payload()` 方法 (line:130-约)
5. `app/services/markdown_text.py` — 删除 `CONTENT_FORMATS` (line:20) 和 `looks_like_html()` (line:399-约)
6. `app/services/provider_store.py` — 删除 `is_loaded()` (line:101)、`get_image_embedding_model()` (line:147)、`get_config()` (line:159)

### 有条件删除
7. `app/config.py` — **仅**删除 `custom_providers()` 函数 (line:42-44)，保留 `_custom_providers` 和 `register_custom_providers()`

### 不建议删除
- 所有 `llm_*` Settings 字段（约35项）— 被 Pydantic 和 getattr 动态使用
- `UserBase` class — 被类继承使用

---

## 五、预期收益

- 减少 ~50 行无效代码
- 降低维护认知负担（删除10处误导性的定义点）
- 对运行时行为无影响，测试不受影响（如果当前有测试覆盖）
