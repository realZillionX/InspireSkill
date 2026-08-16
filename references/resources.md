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

`resources availability`、`resources nodes` 和各 Workload 的 `quota` 是资源事实入口；具体参数和输出以 CLI Help 为准。

`<workload> quota` 回答「有哪些合法的 `gpu,cpu,mem` 档位」，`resources availability` 回答「这些档位现在还有没有空」。**Workspace 的配额天花板不是一个需要规划的约束**：实测 10 个可见 Workspace，GPU 上限要么是 `unlimited`，要么是整个集群容量的两倍（分布式训练空间 10000/20000 对 5589 张卡），要么是 0（那是「这个空间根本没有你的份」，不是配给）；CPU 和内存则处处 `unlimited`。所以先耗尽的永远是硬件，`QUOTA_PENDING` 不会因为这个天花板发生，CLI 也不提供读它的命令。用户级和项目级配额另有一套，需要 Workspace 管理员权限，普通成员读不到。

`Available` 是平台上当前未被占用的 GPU，`Low Pri` 是低优任务占用、可被高优任务抢占的 GPU，`High Pri` 是 `Available + Low Pri`。判断高优任务时不要只看 `Available`，但 `High Pri` 也只是可抢占容量上限；提交后仍以 Events 为准。

`resources policy` 回答另一个方向的问题：拿到手的资源能留多久。每个 Workspace 按 Workload 声明空闲回收规则和运行时长上限，这条命令逐行给出 `Reclaim`（调度器会不会自己收走）、`Idle Rule`（触发条件）和 `Time Limit`（硬上限——Job 是 `max` 运行时长，Notebook 是 `daily` 关机点）。**触发条件是 GPU 利用率，不是有没有人连着**：实测 `分布式训练空间` 对 Job 声明「GPU 低于 40% 持续 3 小时」，对 Notebook 声明「GPU 低于 15% 持续 3 小时，或运行超过 18 小时」，Serving 的规则还按 GPU 档位分条给。所以长时间不吃卡的阶段留在 GPU Workload 里会被无声收走，而这不是故障。`-` 表示这个 Workspace 对该 Workload **没有声明策略**，不等于没有限制，两者结论相反。留任务过夜、跑长训练或让 Serving 常驻之前先读这张表。

余量不够时用 `resources usage` 看余量去了哪儿：它按用户、项目或任务列出存活工作负载持有的算力，以及其中有多少真的在忙（`GPU Busy`）。一大片 GPU 配一个很低的 `GPU Busy` 是资源停着而不是在用，这正是值得去要一下的情况；`--mine` 只看自己的占用，是一次预聚合请求，比扫全 Workspace 便宜。

**`resources` 的每条命令、`<workload> quota` 和 `serving configs` 都只接受一个 Workspace，不接受 `all`。** 配额上限、计算组余量、回收策略和当前占用全部是按 Workspace 定义的事实，它们服务的决定——等、去要、换个地方提交、按什么规格提交——也全部是按 Workspace 做的。跨空间扫一遍不会多回答一个问题，只会把逐空间的行拼在一起再按输出预算截断，让「前 N 名」变成「最先枚举到的那个空间的前 N 名」。要在多个 Workspace 之间比较，就逐个跑，各自读各自的事实。CLI 里还接受 `--workspace all` 的只剩「按名字找东西」那一类：`<workload> list` 和 `account permissions`——不知道东西在哪个空间时，本来就没法先给出空间名。

CLI 为每个账号维护一份本地缓存，只用于加速名称和 Quota 解析；普通 `list`、`status`、`events`、`metrics` 和 Availability 仍然查询 Live 平台，不能把缓存当作资源事实。怀疑缓存过期时用 `inspire cache status|refresh|clear` 管理；清空缓存不会删除任何平台资源。三条命令都支持 `--resource <kind>` 只针对一类，可重复，不带才是全部。kind 分四组：平台目录 `workspace` / `project` / `compute-group` / `image` / `model`，Workload `notebook` / `job` / `hpc` / `ray` / `serving`，Quota 目录 `quota-<workload>`，以及 Notebook 显卡型号那层 `notebook-gpu`。最后这个只能 `status` 和 `clear`，不能 `refresh`——它是用到才探一个组，没有可以整批拉的列表接口。

缓存年龄按东西变多快分三档：Workload 名字 5 分钟，账号结构（`workspace` / `project` / `compute-group` / `model`）1 天，目录类（`image` 和 `quota-<workload>`）7 天。TTL 同时是「这份缓存还能不能读」和「后台多久去补一次」：过期只会让一次解析回落到 Live，那总是安全的；长档换来的风险在另一个方向——平台已经删掉的规格或镜像，可能还会被缓存报出来直到 Scope 过期。管理员刚改过计算组规格、或者刚在网页上删过镜像，用 `inspire cache refresh --resource <kind> [--workspace <name>] --full` 立刻对齐。

空结果只在平台成功回答时才是事实。Quota 目录是「Workspace 里每个 Compute Group 一次请求」的扇出，任何一组没答上（限流、超时、5xx），这一轮就记成 incomplete：已读到的行照常缓存，读不到的那些保留上一轮的旧行，Scope 不算完整刷新。`cache refresh` 会在汇总里报 `N incomplete` 并列出原因，`cache status` 把原因留在该 Scope 的 `error` 上，下一次完整刷新才清掉。因此 `No quota rows found.` 和 `(workspace has no quotas)` 现在只可能来自平台真的返回空；上游没答复时命令直接报 API 错误。命中限流时先重试，持续不缓解再针对性 `inspire cache refresh --resource quota-<workload> --workspace <name> --full`。

## 4. Quota 语义

`--quota` / `-q` 是 `gpu,cpu,mem` 三元组，`mem` 以 GiB 计。GPU 型号不写进三元组，而由 Workspace + Compute Group 决定。

`mem` 表示实例常规内存规格，不是 Shared Memory。GPU Job 的 `/dev/shm` / IPC 空间用 `--shm-size <GiB>` 控制，且不能超过所选 Quota 的 `mem`；提交前用 `job create --dry-run` 确认解析后的 Shared Memory。

三元组必须在当前可见规格里唯一匹配。同一个三元组出现在多个 Compute Group 里是正常现象；先用查询命令按 Group 关键词收窄，再在 `create` 或 Profile 中写完整 Group 名称消歧。

### 优先级合同

`--priority` 有两套合同，由 Workspace 是否开启公平调度决定；CLI 按当前 Workspace 的标记自动选择，不需要也不接受手动指定用哪套：

| Workspace | 可选值 | 默认 |
| --- | --- | --- |
| 公平调度（`分布式训练空间` 是） | `1=LOW` 或 `4=HIGH`，中间值平台不认 | `4` |
| 其他 | `1–10` | `10` |

低优先级任务可以被更高优先级抢占，这是它换来「有空就能起」的代价；公平调度 Workspace 里 `4=HIGH` 就是稳定档。Project 策略还可能把最终优先级再压低一档，所以提交后要从 Status / Events 核实平台真正解析出的值，不要以传入值为准。

### Quota 行的优先级限制

在合同允许的范围之内，有些 Quota 行只接受低优先级。这是平台按 Workspace 逐行声明的调度事实，`<workload> quota` 的 `Priority` 列直接显示它，不再从 Compute Group 名称推断：

| `Priority` | 含义 |
| --- | --- |
| `any` | 平台没有声明限制，`--priority` 只受 Workspace 与 Project 策略约束 |
| `low` | 平台只调度低优先级，公平调度 Workspace 里就是 `--priority 1`，任务可被抢占 |
| `unknown` | 这次没读到平台的调度记录，既没有确认限制也没有排除限制 |

`--json` 输出同时给 `priority` 和 `allowed_priority_levels`；后者的 `[]` 是「无限制」，`null` 是「没读到」，不能当作同一件事。`hpc` 和 `ray` 没有这份声明的 Workspace 一律显示 `any`。

`job` / `notebook` / `serving` 的 `create` 在发出创建请求前按这一列预检：所选行只接受低优先级而 `--priority` 更高时直接报错并说明改法；`unknown` 不阻断创建，因为一次读取失败不等于平台拒绝。

限制按每个 Workload 实例或节点选择的 Quota 判断，不按任务聚合后的 GPU 总数判断。比如每节点 4 GPU、`--nodes 2` 仍是两个碎卡实例，不会因为总计 8 GPU 变成整节点请求；`--nodes` 只放大实例数，不改变单行 Quota 的调度语义。

实测这层限制目前只出现在 `分布式训练空间`，形状是「碎卡只给低优，整节点才给高优」，并且开发区和训练区两片计算组的规则不同：

| Compute Group | `1` / `2` / `4` 卡行 | `8` 卡整节点行 |
| --- | --- | --- |
| `开发区-*` | `any` | `any` |
| `训练区-*` | `low` | `any` |

也就是说：要一个不会被抢占的小规格 GPU 任务，就去开发区；在训练区想拿高优先级，只能整节点起。Notebook、Job 和 Serving 在这个 Workspace 里读到的是同一份限制。这是当前实测结果而不是平台承诺，创建前仍以 `<workload> quota` 的 `Priority` 列为准。

### Quota 行的点券成本

`<workload> quota` 的 `Points/h` 列是该行**每实例每小时**消耗的点券，`--json` 里是 `points_per_hour`。按实例计费，所以 `--nodes 2` 跑 8 点券的行是每小时 16 点券。

**只有 GPU 计费**：所有 CPU-only 行都是 `0`，同一份数据预处理放进 `CPU资源空间` 就不花点券。GPU 按卡型定价，实测 H100 / H200 是 1 点券/卡/小时，4090 是 0.33——差三倍，能跑在 4090 上的活没必要占 H200。

`null`（表里显示 `-`）是「平台没有给这一行定价」，不是免费；两者不能当作同一件事。

### 点券余额有两个，别混

`project list` 给两列，它们是不同的量，而且经常差几个数量级——实测同一个项目里项目余额 233,107、当前账号的额度只有 337：

| 列 | `--json` 键 | 含义 |
| --- | --- | --- |
| `My Budget` | `my_remaining_budget` | **当前账号**在这个项目里还能花多少。这是决定你下一个任务能不能起的那个数 |
| `Project Budget` | `project_remaining_budget` | 项目整体还剩多少，所有成员共用 |

平台不给成员额度时两列相同，此时 `My Budget` 是拿项目余额顶上的，不代表平台真的按人分了额度。

`project detail` 再给花在哪儿：`Spent` 拆成 `on training` / `on inference` / `on storage` / `on private workspace`。逐项目实测它的 `Remaining budget` 与拆分口径一致，所以这是同一个数的展开，不是第二个说法。成员逐人的额度表要 Maintainer 权限，普通成员读到空记录，CLI 不接。

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
| 任务或 Notebook 无故消失、被停 | 先看 Events 有没有抢占记录，再用 `resources policy` 对空闲回收规则和运行时长上限 |

工作负载的 Events 只说平台对**这个任务**做了什么，说不了机器本身发生了什么。`resources node-events <节点名>` 是唯一按节点组织的事件源：内核 OOM kill、`TaskHung`、Cordon / Uncordon、重启、`NodeNotSchedulable`。节点名从 `<workload> instances` 的 `Node` 列或 `<workload> status` 拿；可以一次给多个节点，输出多一列 `Node`。`--from` 按上报组件收窄（`kubelet` / `kernel-monitor` / `node-controller`）。**查不到事件不等于机器没问题**，先核对节点名拼写。
