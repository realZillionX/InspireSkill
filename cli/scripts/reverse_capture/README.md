# Browser API Reverse-Capture Toolkit

`qz.sii.edu.cn` Browser API（`/api/v1/*`）没有公开合约，平台会静默改字段 / 改路径。
这个目录里的工具是**用来重新测一遍现状**的：Playwright 跑一个无头 Chromium，
模拟用户点一圈前端，把每条 `/api/v1/*` 请求/响应吐成 JSONL，然后和 `known_endpoints.py`
里的清单做 diff。

## 什么时候用它

- `references/browser-api.md` 里的某个端点报 `404` / 字段错
- 想加一个 `inspire <x>` 新命令，但不确定后端 body schema 是什么
- 定期（季度 / 半年）体检，确认 CLI 封装的端点都还活着
- 发现了完全没见过的端点（比如前端新加了功能）

## 文件

| 文件 | 用途 |
| --- | --- |
| [capture.py](capture.py) | 主抓包脚本。CAS 登录 → 导航候选路由 → 读-only 交互 → JSONL 输出。严格避开任何 `创建/删除/停止/保存/提交` 按钮 |
| [known_endpoints.py](known_endpoints.py) | 已知端点集 + 归一化（UUID / `ws-` / `nb-` 前缀都折成 `{id}`）+ `STALE_SINCE_2026_04` 退役清单 |
| [analyze.py](analyze.py) | 读 JSONL，打印三段：NEW 端点 / KNOWN now 404 / KNOWN not triggered |

## 准备

```bash
# 仓库根目录
cd cli

# Playwright 本身已在 cli 的 deps 里；浏览器二进制得单独下
uv run playwright install chromium
```

代理：`INSPIRE_PLAYWRIGHT_PROXY` 环境变量或 `--proxy` 参数（默认 `http://127.0.0.1:7897`，与 `references/proxy-setup.md` 对齐）。

## 典型跑法

### 首次（带登录）

```bash
INSPIRE_USERNAME=<学工号> INSPIRE_PASSWORD=<密码> \
  uv run python scripts/reverse_capture/capture.py \
    --out /tmp/bapi.jsonl \
    --save-storage-state /tmp/bapi-session.json
```

CAS 表单有三处陷阱，脚本都处理了：
1. 输入框初始 `visibility:hidden`，`.fill()` 无效 → JS 直接设 `value` + dispatch input/change。
2. 密码走 RSA 加密，`form.submit()` 不触发 `onsubmit`，得原生 `passbutton.click()`。
3. 登录跳 qz → Keycloak broker → CAS → 回 Keycloak → 回 qz 一长串 302，要轮询 URL 归位 + `/api/v1/user/detail` 到 200 才算完成。

### 后续（复用 session）

```bash
uv run python scripts/reverse_capture/capture.py \
  --storage-state /tmp/bapi-session.json \
  --out /tmp/bapi-fresh.jsonl
```

也可以直接吃 InspireSkill CLI 的 session cache：

```bash
uv run python scripts/reverse_capture/capture.py \
  --storage-state ~/.cache/inspire-skill/web_session-<username>.json \
  --out /tmp/bapi-fresh.jsonl
```

**注意**：`web_session-*.json` 是整个 session 的 dump（不仅 storage_state，还带 workspace 元数据）；
`capture.py` 会自动识别两种格式。如果用 CLI cache，TTL 只有 1 小时，过期前完成抓包。

### 指定 workspace 扫

`workspace_id` 放在 `localStorage.spaceId`，URL `?workspace_id=...` 会被忽略。
通过 `--workspace` 让脚本在导航前设 localStorage：

```bash
uv run python scripts/reverse_capture/capture.py \
  --storage-state /tmp/bapi-session.json \
  --workspace ws-6040202d-b785-4b37-98b0-c68d65dd52ce \
  --out /tmp/bapi-gpu.jsonl
```

### 分析结果

```bash
uv run python scripts/reverse_capture/analyze.py /tmp/bapi.jsonl

# 多份 capture 合并分析
uv run python scripts/reverse_capture/analyze.py /tmp/bapi.jsonl /tmp/bapi-gpu.jsonl
```

输出会告诉你：
- **NEW 端点**：这些当前 known set 里没有；如果确认是新的稳定接口，手动加到
  `known_endpoints.py` 并考虑在 CLI / `references/browser-api.md` 里暴露。
- **KNOWN now 404**：后端很可能退役；往 `STALE_SINCE_2026_04` 里挪，并在 CLI 里改走
  替代路径（参考 [notebooks.py 在 2026-04 的迁移](../../inspire/platform/web/browser_api/notebooks.py)）。
- **KNOWN not triggered**：导航没点到 / 是 destructive endpoint。前者可以加路由，
  后者（如 `image/create` / `notebook/operate` / `DELETE image`）默认**不碰**
  以免误创建 / 误删资源。

## 扩展导航

`capture.py` 里的 `DEFAULT_ROUTES` 只覆盖了首页 + 5 大 list 页 + userCenter。
如果需要扫更深（详情页、管理后台），直接改 `sweep()` 加路由。点击策略参考：

- 列表页：`click_first_row` 进第一条 detail（触发 `/{resource}/{id}` + `/events` / `/instance_list` 等懒加载）
- 列表页 / 详情页：`open_and_close_modal` 打开 `+ 新建` 弹窗（触发 `/image/list`、`/logic_compute_groups/list`、`/resource_prices` 等初始化请求），**ESC 关闭不提交**。
- 新加 tab：追加到 `open_and_close_modal` 的 `openers` 列表。

关键防线：`FORBIDDEN_CLICK` 正则，任何文本含 `删除/停止/保存/提交/确认/Delete/Stop/Submit/...` 的按钮都不点。要扩也从这里加。

## 历史背景

本工具是 2026-04 一轮 10 round 抓包后沉淀下来的 —— 当时确认了 Browser API 已悄悄下线了 3 个 CLI 还在调的端点（`GET /notebook/{id}/events`、`GET /notebook/event/{id}`、`POST /notebook/compute_groups`），同时挖出 40+ 条 CLI 当前不封装的新端点（`inference_servings/*`、`model/*`、`user/permissions/{id}`、`workspace/list`、`ssh/*`、`lifecycle/list`、`run_index/list` 等）。详细过程见 [references/browser-api.md](../../../references/browser-api.md) 的"已失效"注释。
