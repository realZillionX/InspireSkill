# Browser API v1 实现地图

> **文档类型**：CLI 维护者参考。日常启智操作不要加载本页；Agent 使用公开命令时只依赖 Name-only CLI 合同和对应 `--help`。
>
> **边界**：平台请求中的不透明键只存在于 [`cli/inspire/platform/web/browser_api/`](../../cli/inspire/platform/web/browser_api/) 和 Session 层。Resolver 从 Name、Workspace、Project、可读候选和本地名称索引完成解析；公开参数、错误、人类输出和 JSON 输出保持 Name-only。

本页只记录当前 CLI 使用的 `/api/v1` 域、认证不变量和公开命令映射。精确请求体与响应解析以同目录 Wrapper 和测试为准；长期文档只保留可复现且有当前消费者的事实。

同一 Session 上的 `/api/v2` Action 面单独记在 [`browser-api-v2.md`](browser-api-v2.md)：两代接口共用本页第 2 节的 Session 与账号隔离不变量，但 URL 形状、响应信封、错误语义和权限边界都不同。已经迁到 v2 的域在第 3 节标注为 v2，细节不在本页展开。

## 1. 事实源

维护顺序：

1. 当前 Click Help 定义公开 CLI 合同。
2. [`cli/inspire/platform/web/browser_api/`](../../cli/inspire/platform/web/browser_api/) 定义平台请求和响应归一化。
3. [`cli/inspire/platform/web/session/`](../../cli/inspire/platform/web/session/) 定义账号隔离、登录、Cookie、代理和 Workspace 解析。
4. `cli/tests/` 锁定公开 Name-only 输出与内部 Wrapper 合同；`test_browser_api_boundary.py` 另外保证平台路径只在本目录内构造——命令层出现 `_browser_api_path` 或 `/api/v1` 字面量会直接让 CI 失败，例外必须在该测试的 `_ALLOWED` 里写明理由。
5. 平台行为变化时，用脱敏 `inspire --debug ...`、浏览器 DevTools 或受控写操作核对，再同步修改 Wrapper、测试和本页。

不要在本页积累尚未消费的端点猜测。没有当前 CLI 或维护 Helper 消费者的信息应留在一次性调查记录中，验证后立即实现或删除。

## 2. 认证与请求不变量

本节的 Session、账号隔离和输出边界对 v1 和 v2 同时成立。

- Browser API v1 默认前缀是 `/api/v1`，由 `browser_api_prefix` 配置。**平台调用面现在基本都在 `/api/v2`**（83 个 Action 调用点 / 11 条路由），v1 只剩第 3 节标出的三处，契约见 [`browser-api-v2.md`](browser-api-v2.md)。
- 所有平台请求使用目标 Account Alias 对应的浏览器 SSO Session；跨账号 Notebook 命令不能退回当前活动账号的 Session。
- 请求需要使用与页面域匹配的 `Referer`。各 Wrapper 负责构造，调用方不要自行拼接。
- Base URL、Browser API Prefix 和代理来自当前有效配置；Playwright 登录与后续请求复用同一账号网络设置。
- Browser API 的平台原始响应不得直接穿透到公共输出。命令层必须先解析、投影和清洗。
- 写操作需要明确的受控验证；只读流量不能用来推导创建、启动、停止、保存或删除语义。

## 3. 当前公开命令映射

| 域 | 当前 Browser API 家族 | 公开 CLI |
| --- | --- | --- |
| 账号与用户 | 全部已迁 v2 | `account permissions` |
| Workspace 与 Project | 全部已迁 v2 | `config context`, `init`, `project list/detail/owners` |
| 文件页发现 | 全部已迁 v2 | `init`, `init --scope project`, Notebook Path Alias 工作流 |
| Notebook | 全部已迁 v2；Notebook Lab 与 Proxy 见下一行，仍在 v1 | `notebook create/list/status/start/stop/delete/events/lifecycle`, Name Resolver |
| Notebook 终端与 Proxy | 仅剩 Notebook Proxy（反向代理，见 v2 文档第 9 节）；Terminal 已改走纯 HTTP + Python WebSocket，`/notebook/lab*` 只作浏览器回落 | `notebook exec/shell/proxy-url`, 支持 SSH 的 Notebook 的 `ssh`/`scp`/`ssh-config` |
| Image | 全部已迁 v2 | `image list/detail/register/save/set-visibility/delete` |
| GPU Job | 仅剩 `/train_job/remote_cmd`（双向 PTY 流，不是 Action 能表达的东西）| `job create/list/status/stop/delete/events/instances/logs/command/shell/wait`, Name Resolver |
| HPC | 全部已迁 v2；按当前用户过滤列表时复用账号域的 `user.GetUserDetail` | `hpc create/list/status/stop/delete/events/instances`, Name Resolver |
| Ray | 全部已迁 v2；按当前用户过滤列表时复用账号域的 `user.GetUserDetail` | `ray create/list/status/stop/delete/events/instances`, Name Resolver |
| 资源与 Quota | 仅剩 `/resource_prices/logic_compute_groups/`（换 v2 更贵，见 v2 文档第 9 节）；计算组、节点维度与组资源统计已迁 v2 | `resources availability/nodes`, `notebook/job/hpc/ray/serving quota`, 创建命令的 Group 与 Quota 解析 |
| Metrics | 全部已迁 v2 | `notebook/job/hpc/ray/serving metrics` |
| Model Registry | 全部已迁 v2 | `model list/status/versions/register`, Serving 的 Model 解析 |
| Serving | 全部已迁 v2 | `serving configs/create/list/status/start/stop/delete/events/instances`, Name Resolver |

Batch 和 Workload Profile 不引入新的平台接口：Batch 展开后复用对应 `create`，Profile 只保存 `workspace`、`project`、`group`、`quota` 和 `image` 名称。

## 4. Metrics 合同

Metrics Wrapper 统一覆盖 Notebook、Job、HPC、Ray 和 Serving，并负责：

- 从资源详情解析内部查询键与 Compute Group。
- 将多个指标拆成平台接受的请求，再合并为统一结果。
- 归一化时间窗、采样间隔、单位和 Per-Instance 分组。
- 在命令层只输出可读资源名、实例名、统计摘要和可选的有界原始样本。

公开命令共享同一组选项和输出投影；新增资源类型时必须同时更新 `TASK_TYPE_BY_RESOURCE`、Resolver、测试和 Help。

## 5. Notebook Transport

- 受限 Notebook 的 `exec` / `shell` 使用 Jupyter Terminal REST + WebSocket，全程不起浏览器（lab URL 来自 `notebook.GetNotebookAccessUrl`，`_xsrf` 来自一次普通 GET），并在命令结束后回收本次创建的 Terminal。
- 受限与否读机器上的 `nvidia-smi`，走同一条 Jupyter Terminal 通道；显卡不是 `H100` / `H200` 的 Notebook 可以建立本地 Connection，供 `ssh`、`scp`、`ssh-config` 和外部 OpenSSH 工具复用。同一个 `notebook_id` 只探测一次，结果存 `~/.inspire/notebook-gpu-models.json`。
- `--account` 指定的 Account Alias 必须贯穿 Name 解析、Session、代理、Terminal 和 Connection Cache。
- `proxy-url` 是整个 Notebook 命令组里**唯一**打印平台 URL 的命令：Agent 要靠它去请求容器里的服务，而这个地址的每一段都是平台句柄，洗过就不通了。它走 `format_json(..., preserve_raw={"url"})` 这个显式开关，不是绕过输出边界。其余命令一律不把内部网关路径当作公共资源身份。

## 6. 变更验收

Browser API 变更至少完成：

1. Wrapper 只暴露调用方需要的最小归一化数据。
2. 命令使用 Name 输入，并在同名时提供可读候选与 `--pick`。
3. Human 与 JSON 输出使用显式 Allowlist，不透传原始响应。
4. Help、错误、事件流、SSH / PTY 输出和 Debug 摘要通过输出边界测试。
5. 对应命令 Help、Wrapper 测试和本页映射同步更新。

未闭合的调查结果不进入长期 Reference。

把某个域从 v1 换到 v2 时，除上述五条外还要满足 [`browser-api-v2.md`](browser-api-v2.md) 的迁移验收，并把本页第 3 节对应行改成 v2 标注。
