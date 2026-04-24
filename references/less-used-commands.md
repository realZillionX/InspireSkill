# 较少使用 / 权限受限的 CLI 命令

本文档收录 [`SKILL.md`](../SKILL.md) 主速查表**不包含**的 CLI 命令——不是不存在，而是日常科研开发用不到、或者普通账号没权限、或者 Web UI 一眼就看完更省事。需要时翻这里。

SKILL.md 的命令速查只覆盖真正天天用的东西，把它的上下文窗口留给调度语义和开发流程。

---

## 1. 模型部署（`inspire serving`）

> **权限要求**：账号需有 `inference_serving.create`（或等价的部署权限）。普通用户（只有 `job.trainingJob.create` + `job.notebook.create` 那两条的）点网页 "部署服务" 按钮会**被静默踢回首页**，CLI 侧 `create` 也会失败。没权限跳过本节。
>
> **端点拼法**：`list` / `configs` 只在 Browser API（SSO cookie）；`status` / `stop` OpenAPI 和 Browser API 都有，CLI 选 OpenAPI 以跟 `job` / `hpc` 同构。建 serving 因为必填字段太多（`model_id` / `port` / `replicas` / `spec_id` / `logic_compute_group_id` / ...）CLI 侧没包 `create` —— 直接走 Web UI `/jobs/modelDeployment` 或打 `POST /openapi/v1/inference_servings/create`。

| 命令 | 用途 |
| --- | --- |
| `inspire serving list [-a\|--all]` | 默认只列当前用户的部署；`-a` 列 workspace 全量 |
| `inspire serving status <serving-id>` | 单个部署详情（镜像 / 副本数 / 模型版本 / 创建时间） |
| `inspire serving stop <serving-id>` | 止损 |
| `inspire serving configs` | workspace 可用镜像 / 规格组合（建部署时下拉选项的数据源） |
| `inspire serving metrics <name>` | 多副本部署的资源视图时间序列，和 `inspire notebook/job/hpc metrics` 同 UX（默认出 PNG 到 `~/.inspire/metrics/serving-<id>-<unix>.png` + per-replica stats + straggler 检测）；看哪个 replica 吃显存更多、是否均匀负载。lcg 自动从 `inference_servings/detail` 解。 |

---

## 2. 模型注册表（`inspire model`）

> **只读浏览**。创建 / 发版 / 删除一律只能走 Web UI `/modelLibrary`（Browser API 的 `POST /model/create` 没被 CLI 封装）。
>
> 多数情况下 Web UI 的模型库列表更直观；CLI 适合需要脚本化 / `--json` 消费时用。

| 命令 | 用途 |
| --- | --- |
| `inspire model list` | workspace 下所有模型：`vLLM` 兼容标记、最新版本号、创建时间 |
| `inspire model status <model-id>` | 单模型详情：项目归属 / 负责人 / `vLLM` 就绪标志 / 发布状态 |
| `inspire model versions <model-id>` | 版本清单：版本号 / 大小 / 路径 |

**`list` 与 `status` 的 `is_vllm_compatible` 可能不一致**：平台的 `list` 端点把最新版本属性合进了模型条目，`detail` 端点只返回模型主记录，两处 flag 对不上是平台侧响应行为，非 CLI bug。要准确的版本属性用 `versions`。

---

## 3. 项目详情 / Owner 清单

> 日常 `inspire project list` 够用（配额、优先级、预算都在）。下面两条只在建任务前想确认负责人下拉、或者查某个项目的具体预算拆分时用。

| 命令 | 用途 |
| --- | --- |
| `inspire project detail <project-id>` | 单项目详情：预算 / 子项目预算 / 优先级 / 创建人 |
| `inspire project owners` | 全局"负责人"下拉清单（建任务时选归属人用） |

---

## 4. 用户配额 / API Key 元数据

> **`user quota` 对普通账号不可用**：端点在平台侧是 admin-gated，CLI 会收到 `用户不存在`；CLI 会打一行 hint 提示改用 `inspire project list` 看每个项目的剩余预算和 GPU 上限。
>
> **`user api-keys` 只是 metadata**：不会返回 key 值（值只在创建时一次性下发）；创建 / 删除走 Web UI `/userCenter`。

| 命令 | 用途 |
| --- | --- |
| `inspire user quota` | 用户级配额（**admin-only**，普通账号会失败） |
| `inspire user api-keys` | 列自己已创建的 API Key 元数据 |
