# 资源与调度条件

选择 Workspace、Project、Compute Group、`--quota`、镜像和 Workload Profile 时先看本页。共享盘、存储池和 Path Alias 看 [`paths.md`](paths.md)；联网准备和内部源看 [`internal-sources.md`](internal-sources.md)。具体命令表面始终回到 CLI Help。

## 1. 三类名字

启智任务先分清三类名字：

| 类型 | 决定什么 | 典型字段 |
| --- | --- | --- |
| 调度条件 | 任务在哪跑、用多少资源、基于哪个镜像 | `workspace`、`project`、`group`、`quota`、`image` |
| 远端路径 | 代码、数据、权重、Checkpoint 和产物放在哪 | `me`、`public`、`ssd.me`、`qb-ilm2.public` |
| 对象名字 | 观察、连接或清理哪个平台对象 | Notebook / Job / HPC / Ray / Serving 的名称 |

调度条件没有隐式默认值。创建 Workload 时显式传入，或用 Workload Profile 保存这五类条件。Path Alias 只表示远端路径，不能替代 Workspace、Project、Group、Quota 或 Image。

## 2. Workspace 判断

`CPU资源空间` 和 `分布式训练空间` 是所有用户默认可用的公共 Workspace，日常任务直接按职责选择：

| Workspace | 主要职责 |
| --- | --- |
| `CPU资源空间` | CPU Notebook、联网准备、依赖安装、CPU HPC、CPU Ray |
| `分布式训练空间` | GPU Notebook、GPU Job、多节点训练、Serving、GPU 指标观察 |

其余 Workspace（项目专属空间、国产卡分区等）都必须由用户亲自指认后才能使用；已指认的专属 Workspace 及其职责记录在项目上下文（见 [`project-context.md`](project-context.md)），不要因为列表里可见就自行启用。

## 3. Resource Truth

资源事实来自 Live 查询；判断顺序：

1. 先看账号当前可见的 Workspace、Project 和 Compute Group 名字。
2. 按 Workload 类型查对应 Quota：CPU Notebook / HPC / CPU Ray 在 `CPU资源空间`，GPU Notebook / Job / Serving 在 `分布式训练空间`。
3. 用实时 Availability 判断空余；多节点 GPU 任务再看整节点空闲。
4. 创建命令里的 `--group` 使用完整 Compute Group 名称；查询命令里的 Group Filter 可以用关键词收窄候选。

`resources availability`、`resources nodes`、`resources quota` 和各 Workload 的 `quota` 是资源事实入口；具体参数和输出以 CLI Help 为准。

`<workload> quota` 和 `resources quota` 回答的不是同一个问题：前者是「有哪些合法的 `gpu,cpu,mem` 档位」，后者是「这个 Workspace 还允许占多少、集群还剩多少」。两者都会拒绝任务，失败形态不同——配额用尽停在 `QUOTA_PENDING`，集群占满停在 `PENDING` 并伴随 `FailedScheduling` 事件。大规模提交前两个都看。用户级和项目级配额需要 Workspace 管理员权限，普通成员读不到。

`Available` 是平台上当前未被占用的 GPU，`Low Pri` 是低优任务占用、可被高优任务抢占的 GPU，`High Pri` 是 `Available + Low Pri`。判断高优任务时不要只看 `Available`，但 `High Pri` 也只是可抢占容量上限；提交后仍以 Events 为准。

余量不够时用 `resources usage` 看余量去了哪儿：它按用户、项目或任务列出存活工作负载持有的算力，以及其中有多少真的在忙（`GPU Busy`）。一大片 GPU 配一个很低的 `GPU Busy` 是资源停着而不是在用，这正是值得去要一下的情况；`--mine` 只看自己的占用，是一次预聚合请求，比扫全 Workspace 便宜。**它只接受一个 Workspace，不接受 `all`**：配额和调度都是按 Workspace 走的，等、去要、还是换个地方提交这三个决定也都是；而聚合是按 Workspace 分桶的，扇出只会给出「(空间, 人) 组合」的排名而看起来像全平台排名。跨 Workspace 找地方仍然是 `resources availability` 和 `resources nodes` 的活。

CLI 为每个账号维护一份本地缓存，只用于加速名称和 Quota 解析；普通 `list`、`status`、`events`、`metrics` 和 Availability 仍然查询 Live 平台，不能把缓存当作资源事实。怀疑缓存过期时用 `inspire cache status|refresh|clear` 管理；清空缓存不会删除任何平台资源。三条命令都支持 `--resource <kind>` 只针对一类，可重复，不带才是全部。kind 分四组：平台目录 `workspace` / `project` / `compute-group` / `image` / `model`，Workload `notebook` / `job` / `hpc` / `ray` / `serving`，Quota 目录 `quota-<workload>`，以及 Notebook 显卡型号那层 `notebook-gpu`。最后这个只能 `status` 和 `clear`，不能 `refresh`——它是用到才探一个组，没有可以整批拉的列表接口。

空结果只在平台成功回答时才是事实。Quota 目录是「Workspace 里每个 Compute Group 一次请求」的扇出，任何一组没答上（限流、超时、5xx），这一轮就记成 incomplete：已读到的行照常缓存，读不到的那些保留上一轮的旧行，Scope 不算完整刷新。`cache refresh` 会在汇总里报 `N incomplete` 并列出原因，`cache status` 把原因留在该 Scope 的 `error` 上，下一次完整刷新才清掉。因此 `No quota rows found.` 和 `(workspace has no quotas)` 现在只可能来自平台真的返回空；上游没答复时命令直接报 API 错误。命中限流时先重试，持续不缓解再针对性 `inspire cache refresh --resource quota-<workload> --workspace <name> --full`。

## 4. Quota 语义

`--quota` / `-q` 是 `gpu,cpu,mem` 三元组，`mem` 以 GiB 计。GPU 型号不写进三元组，而由 Workspace + Compute Group 决定。

`mem` 表示实例常规内存规格，不是 Shared Memory。GPU Job 的 `/dev/shm` / IPC 空间用 `--shm-size <GiB>` 控制，且不能超过所选 Quota 的 `mem`；提交前用 `job create --dry-run` 确认解析后的 Shared Memory。

三元组必须在当前可见规格里唯一匹配。同一个三元组出现在多个 Compute Group 里是正常现象；先用查询命令按 Group 关键词收窄，再在 `create` 或 Profile 中写完整 Group 名称消歧。

### Quota 行的优先级限制

有些 Quota 行只接受低优先级。这是平台按 Workspace 逐行声明的调度事实，`<workload> quota` 的 `Priority` 列直接显示它，不再从 Compute Group 名称推断：

| `Priority` | 含义 |
| --- | --- |
| `any` | 平台没有声明限制，`--priority` 只受 Workspace 与 Project 策略约束 |
| `low` | 平台只调度低优先级，公平调度 Workspace 里就是 `--priority 1`，任务可被抢占 |
| `unknown` | 这次没读到平台的调度记录，既没有确认限制也没有排除限制 |

`--json` 输出同时给 `priority` 和 `allowed_priority_levels`；后者的 `[]` 是「无限制」，`null` 是「没读到」，不能当作同一件事。`hpc` 和 `ray` 没有这份声明的 Workspace 一律显示 `any`。

`job` / `notebook` / `serving` 的 `create` 在发出创建请求前按这一列预检：所选行只接受低优先级而 `--priority` 更高时直接报错并说明改法；`unknown` 不阻断创建，因为一次读取失败不等于平台拒绝。

限制按每个 Workload 实例或节点选择的 Quota 判断，不按任务聚合后的 GPU 总数判断。比如每节点 4 GPU、`--nodes 2` 仍是两个碎卡实例，不会因为总计 8 GPU 变成整节点请求；`--nodes` 只放大实例数，不改变单行 Quota 的调度语义。同一个 Compute Group 里更大的整节点行往往就是 `any`。

具体可用 GPU 型号、机房和 `gpu,cpu,mem` 三元组仍以当前 Workload 的 Live Quota Row 为准；创建 Workload 或写 Profile 时从同一行复制完整 `group` 和 `quota`。提交后再从 Status / Events 核实平台解析出的优先级、排队和抢占结果。

申请资源前按真实任务需求和实时空余选择规格。不要因为猜测主动降档；只有调度语义、空余量或项目策略明确不足时再缩小规模。

## 5. Workload Profile

Profile 是调度条件组 Alias，只保存 `workspace`、`project`、`group`、`quota` 和 `image`。它不是 Path Alias，也不是远端工作目录。

适合写 Profile 的场景：

- 同一个项目反复创建同规格 GPU Probe、训练 Job 或 Serving。
- 同一批 Batch 条目共用调度条件，只变名称、命令或输入输出路径。

不适合写 Profile 的场景：

- 只想给远端目录起名字。用 Path Alias。
- 资源只用一次，且当前任务还在探索。
- 想省略 Workspace。没有默认 Workspace；Profile 也必须明确 Workspace。

## 6. 调度与资源观察

创建前看 Quota 和 Availability；提交后先看 Events，再看 Logs / Metrics / Instances。`status=RUNNING` 只说明平台对象在运行，不说明业务健康；`status=SUCCEEDED` 也不说明产物完整。

常见判断：

| 现象 | 优先方向 |
| --- | --- |
| 0 候选或 Quota Match Failed | Workspace / Group / Quota 三元组不匹配 |
| PENDING 很久 | 实时资源不足、优先级不足、节点条件不满足 |
| RUNNING 但业务没推进 | 看 Metrics 是否有 GPU / CPU / I/O 负载，再回到日志和产物 |
| 多节点某个 Worker 掉队 | 先看 Per-Instance Metrics 和 Instances，再看该 Worker 日志 |
| 同一台机器上反复失败 | `resources node-events <节点名>` 看这台机器自己的事实 |

工作负载的 Events 只说平台对**这个任务**做了什么，说不了机器本身发生了什么。`resources node-events <节点名>` 是唯一按节点组织的事件源：内核 OOM kill、`TaskHung`、Cordon / Uncordon、重启、`NodeNotSchedulable`。节点名从 `<workload> instances` 的 `Node` 列或 `<workload> status` 拿；可以一次给多个节点，输出多一列 `Node`。`--from` 按上报组件收窄（`kubelet` / `kernel-monitor` / `node-controller`）。**查不到事件不等于机器没问题**，先核对节点名拼写。
