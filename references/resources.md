# 资源与调度条件

选择 Workspace、Project、Compute Group、`--quota` 和镜像时先看本页。共享盘、存储池和绝对路径看 [`paths.md`](paths.md)；联网准备和内部源看 [`internal-sources.md`](internal-sources.md)。具体命令表面始终回到 CLI Help。

## 1. 三类名字

启智任务先分清三类名字：

| 类型 | 决定什么 | 典型字段 |
| --- | --- | --- |
| 调度条件 | 任务在哪跑、用多少资源、基于哪个镜像 | `workspace`、`project`、`group`、`quota`、`image` |
| 远端路径 | 代码、数据、权重、Checkpoint 和产物放在哪 | `/inspire/<tier>/project/...` 绝对路径 |
| 对象名字 | 观察、连接或清理哪个平台对象 | Notebook / Job / HPC / Ray / Serving 的名称 |

调度条件没有隐式默认值。创建 Workload 时每次显式传入这五项；CLI 不保存仓库绑定或 Workload Profile。

## 2. Workspace 判断

`CPU资源空间` 和 `分布式训练空间` 是所有用户默认可用的公共 Workspace，日常任务直接按职责选择：

| Workspace | 主要职责 |
| --- | --- |
| `CPU资源空间` | CPU Notebook、联网准备、依赖安装、CPU HPC、CPU Ray |
| `分布式训练空间` | GPU Notebook、GPU Job、多节点训练、Serving、GPU 指标观察 |

其余 Workspace（项目专属空间、国产卡分区等）都必须由用户亲自指认后才能使用；不要因为列表里可见就自行启用。

## 3. Resource Truth

资源事实来自 Live 查询；判断顺序：

1. 先看账号当前可见的 Workspace、Project 和 Compute Group 名字。
2. 按 Workload 类型查对应 Quota：CPU Notebook / HPC / CPU Ray 在 `CPU资源空间`，GPU Notebook / Job / Serving 在 `分布式训练空间`。
3. 用实时 Availability 判断保障额度余量；多节点 Job 只能按每节点 8 卡提交，再用 `resources nodes` 看当前完全空闲和清退低优任务后的 8 卡整节点数。
4. 创建命令里的 `--group` 使用完整 Compute Group 名称；查询命令里的 Group Filter 可以用关键词收窄候选。

`resources availability`、`resources nodes` 和各 Workload 的 `quota` 是资源事实入口；具体参数和输出以 CLI Help 为准。

`<workload> quota` 回答「有哪些合法的 `gpu,cpu,mem` 档位」，`resources availability` 回答「这些档位现在还有没有空」。CLI 不展示 Workspace 级配额天花板，因为创建决策落在具体 Compute Group 的 Live Quota Row 与实时容量上；用户级和项目级上限属于另一套管理员视图。创建被拒时以 Events 和平台错误区分硬件不足、调度策略、项目预算与权限，不能用某个账号曾看到的集群总量或上限推断。

`Available` 是 Compute Group 分给当前 Workspace 的保障额度余量（`gpu_total - gpu_used`），不是整组物理节点上的空卡数；公平调度允许超出保障额度运行低优任务，所以它可以为负。`low_priority_gpus`（JSON）是低优任务占用、可被高优任务抢占的 GPU，Human 表里的 `High Pri` 是 `Available + low_priority_gpus`，同样可能在已超保障时为负。判断高优任务时不要只看 `Available`；要判断物理整节点看 `resources nodes`：`Free Now` 是当前完全空闲，`High Pri` 再加上所有占用都明确为低优的可抢占整节点，`Idle GPUs` 严格等于 `Free Now × 8`，不读取保障额度余量。提交后仍以 Events 为准。GPU 组也不能靠 `gpu_total > 0` 识别：保障为 0 的组仍可能有 GPU 节点和正在运行的 GPU 任务，CLI 综合 Live 使用量、型号和 NodeDimension 分类。

`resources policy` 回答另一个方向的问题：拿到手的资源能留多久。每个 Workspace 按 Workload 声明空闲回收规则和运行时长上限，这条命令逐行给出 `Reclaim`（调度器会不会自己收走）、`Idle Rule`（触发条件）和 `Time Limit`（硬上限——Job 是 `max` 运行时长，Notebook 是 `daily` 关机点）。触发条件按平台返回的 GPU 利用率与时间规则判断，不按有没有人连着判断；Serving 还可能按 GPU 档位分条。`-` 表示这个 Workspace 对该 Workload **没有声明策略**，不等于没有限制。留任务过夜、跑长训练或让 Serving 常驻之前读取当前 Workspace 的 Live 策略。

余量不够时用 `resources usage` 看余量去了哪儿：它按用户、项目或任务列出存活工作负载持有的算力，其中 `Reclaimable` 是这些卡里有多少落在**以可抢占优先级提交**的任务上——那部分高优任务可以直接拿走，剩下的只能等或者去谈。`--by task` 的 `Prio` 列给每个任务提交时的优先级原值。`--mine` 会把当前登录用户 id 传给 UserDimension，只读自己的预聚合项目行；该接口不带用户过滤时返回 Workspace 全员，不能直接叫“我的占用”。

这三张资源视图始终读 Live，但会在一次命令内部避免重复：Availability 对多个 Compute Group 有界并发；Nodes 复用 Availability 已读到的 NodeDimension，再读取一次 TaskDimension 取得任务提交优先级，并额外读取规格；Usage 的维度列表显式使用网关允许的 5000 行页，超过时才继续翻页。命令结束后这些实时行不会进入持久缓存。

低优判据跟着 Workspace 的优先级合同走：公平调度空间里小于 `4` 算低优，其余空间 `≤3` 算低优。**读不到合同时这一列是 `-` 而不是 `0`**——「没有可抢的」和「不知道能不能抢」会导向相反的决定。

**`resources usage` 的 `Reclaimable` 和 `resources availability` 的 `low_priority_gpus` 不保证对得上。** 后者是平台按计算组实时计算的，是「到底能抢多少卡」的权威；前者是客户端把持有量按每个任务提交时的优先级归到人头上。两次查询的时间点和公平调度口径都可能造成差异。要判断能抢多少卡看 `availability` 的 `High Pri` 与 `Available`，要判断该找谁谈看 `usage`。整节点能否在清退后腾空则看 `nodes` 的 `High Pri`；混有高优任务、优先级缺失或两次 Live 快照之间发生变化的节点都会保守地不算可抢占。

利用率不再进表：卡在谁手里跟它忙不忙没关系，忙不忙只是「该不该请人释放」这个另一个问题的论据。`--json` 里 `gpu_usage_rate` 照旧给。

**`--group` 收窄到计算组**，也就是任务真正提交进去的那个单位——整个 Workspace 看着满，你要投的那个组未必满，反过来也一样，所以「等还是去要」这个判断本来就该在组这一层做。关键词是子串，`--group H200` 会覆盖所有带这个硬件的组，输出顶部列出实际匹配到哪几个。`--mine` 读的是按项目预聚合的记录，里面根本没有计算组，两个选项不能一起用。

**`resources` 的每条命令、`<workload> quota` 和 `serving configs` 都只接受一个 Workspace，不接受 `all`。** 配额上限、计算组余量、回收策略和当前占用全部是按 Workspace 定义的事实，它们服务的决定——等、去要、换个地方提交、按什么规格提交——也全部是按 Workspace 做的。跨空间扫一遍不会多回答一个问题，只会把逐空间的行拼在一起再按输出预算截断，让「前 N 名」变成「最先枚举到的那个空间的前 N 名」。要在多个 Workspace 之间比较，就逐个跑，各自读各自的事实。CLI 里还接受 `--workspace all` 的只剩「按名字找东西」那一类：`<workload> list` 和 `account permissions`——不知道东西在哪个空间时，本来就没法先给出空间名。

CLI 为每个账号维护一份本地缓存，只用于加速名称和 Quota 解析；普通 `list`、`status`、`events`、`metrics` 和 Availability 仍然查询 Live 平台，不能把缓存当作资源事实。怀疑缓存过期时用 `inspire cache status|refresh|clear` 管理；清空缓存不会删除任何平台资源。三条命令都支持 `--resource <kind>` 只针对一类，可重复，不带才是全部。kind 分四组：平台目录 `workspace` / `project` / `compute-group` / `image` / `model`，Workload `notebook` / `job` / `hpc` / `ray` / `serving`，Quota 目录 `quota-<workload>`，以及 Notebook 显卡型号那层 `notebook-gpu`。最后这个只能 `status` 和 `clear`，不能 `refresh`——它是用到才探一个组，没有可以整批拉的列表接口。

缓存年龄按东西变多快分三档：Workload 名字和账号结构（`workspace` / `project` / `compute-group` / `model`）1 天，目录类（`image` 和 `quota-<workload>`）7 天。Workload 的状态虽然变化快，但缓存里只保存稳定的「名字 → 句柄」映射；普通 `status` / `list` 仍然读 Live。创建和删除经过 CLI 时会立即写入或墓碑，缓存 miss、过期和 stale-handle 重试只针对当前名字回源。因此平台在网页上删除并用同名重建对象时，最坏是第一次操作命中旧句柄、多付一次 Live 重试，不会把旧状态当成当前事实。

**普通命令不会启动全账号后台刷新。** 缓存只由实际解析按需填充；没有消费者的 Scope 不产生网络流量。这样在多 Workspace 的慢账号上不会为未使用的 Image、Model 和 Workload 逐空间预热，也不会让缓存维护与前台争用同一账号的请求额度。

**`inspire cache refresh` 必须说明刷什么**，不带 `--resource` / `--workspace` / `--name` 会直接报错。全量刷一遍是几百个请求，而且读的是几乎不动的目录；正常情况下你根本不需要跑它。真正要跑的场合只有一种：你知道缓存底下的东西变了，比如管理员刚改过计算组规格、或者刚在网页上删过镜像。这时候先 `inspire cache status` 看哪个 Scope 真的不对，再 `inspire cache refresh --resource <kind> --workspace <name>` 只刷那一块。每次显式 refresh 都是完整对账，会把平台不再列出的行清掉；旧版的冗余 `--full` 仍被静默接受，但不再出现在 Help。

Workload 历史目录超过 5000 行时不会硬扫：刷新器按平台报告的 `total` 和实际累计行数分别设限，超限时保留已有名称行。Job / Serving / TensorBoard 的列表支持服务端名字过滤，`--name` 可以把读取收窄；HPC / Ray 不支持，点名刷新仍可能面对整份历史并被上限挡下。把大型历史目录完整搬进本机 SQLite 既慢也没有消费者；正常命令仍按当前名字读穿并写回，后续同名操作直接命中本地缓存。

`cache status` 里的 `partial` 表示这个 Scope 只经过某个名字的定向解析、尚未做完整扫描。这不是故障，`error` 才是；需要完整对账时显式运行上述窄范围 `cache refresh`。

`cache status` / `cache clear` 管的是资源名称索引、Quota 行和 Notebook GPU 探测结果；不碰登录 Session、IDE/连接目标、代理状态或更新检查状态。`status` 只汇总当前 Session 的身份分区，避免同一本地账号配置切换登录主体后把旧分区的数量和错误混进来；`clear` 仍清当前账号的全部分区。默认 `cache clear --yes` 所说的 “every managed cache” 仅指这些明确列在 `cache status` 里的缓存，不能拿它当登出或网络重置命令。

`empty` 是另一个要看的信号：这个资源**刷新过、还在有效期内，却一个名字都拿不出来**。单个 Workspace 空是正常的（那个空间就是没有 Notebook），所以这个判定只按整个资源汇总——全局一条都没有，而刷新又声称跑过。撞到 `empty` 先 `cache refresh --resource <kind>`；完整刷新仍为空时，再把它当作当前平台事实或账号可见性问题处理。

空结果只在平台成功回答时才是事实。Quota 目录是「Workspace 里每个 Compute Group 一次请求」的扇出，任何一组没答上（限流、超时、5xx），这一轮就记成 incomplete：已读到的行照常缓存，读不到的那些保留上一轮的旧行，Scope 不算完整刷新。`cache refresh` 会在汇总里报 `N incomplete` 并列出原因，`cache status` 把原因留在该 Scope 的 `error` 上，下一次完整刷新才清掉。因此 `No quota rows found.` 和 `(workspace has no quotas)` 现在只可能来自平台真的返回空；上游没答复时命令直接报 API 错误。命中限流时先重试，持续不缓解再针对性 `inspire cache refresh --resource quota-<workload> --workspace <name>`。

刷新错误只保留一行、最多 500 个字符的诊断摘要；Cookie、Authorization、URL、账号路径和平台路径在写进 SQLite 前就会被抹掉。新版第一次打开旧索引时也会原地清理历史错误，不需要清空仍然有效的名称行。

## 4. Quota 语义

`--quota` / `-q` 是 `gpu,cpu,mem` 三元组，`mem` 以 GiB 计。GPU 型号不写进三元组，而由 Workspace + Compute Group 决定。

`mem` 表示实例常规内存规格，不是 Shared Memory。GPU Job 的 `/dev/shm` / IPC 空间用 `--shm-size <GiB>` 控制，且不能超过所选 Quota 的 `mem`；提交前用 `job create --dry-run` 确认解析后的 Shared Memory。

三元组必须在当前可见规格里唯一匹配。同一个三元组出现在多个 Compute Group 里是正常现象；先用查询命令按 Group 关键词收窄，再在 `create` 或 Profile 中写完整 Group 名称消歧。

### 优先级合同

`--priority` 有两套合同，由 Workspace 是否开启公平调度决定；CLI 按当前 Workspace 的标记自动选择，不需要也不接受手动指定用哪套：

| Workspace | 可选值 | 默认 |
| --- | --- | --- |
| 公平调度 Workspace | `1=LOW` 或 `4=HIGH`，中间值平台不认 | `4` |
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

限制按每个 Workload 实例或节点选择的 Quota 判断，不按任务聚合后的 GPU 总数判断。对于 Job，`--nodes n`（`n > 1`）只接受每个节点 8 GPU 的满节点 Quota，因此总规模只能是 `n × 8`；不能用 2 / 4 / 6 GPU Quota 创建 `n × 2`、`n × 4`、`n × 6` 多节点任务。`--nodes` 只放大 8 卡 Instance 数，不会把碎卡规格合并成多节点请求。

限制可能随 Workspace、Compute Group、Workload 与单实例规格变化；碎卡和整节点行也不保证使用相同策略。不要从 Group 名称或 GPU 数量猜，创建前始终以目标 Workload 的 Live `<workload> quota` `Priority` 列为准。

### Quota 行的点券成本

`<workload> quota` 的 `Points/h` 列是该行**每实例每小时**消耗的点券，`--json` 里是 `points_per_hour`。按实例计费，所以 `--nodes 2` 跑 8 点券的行是每小时 16 点券。

平台当前返回的 CPU-only 行价格为 `0`；GPU 行按卡型和规格给出 Live 价格。不要把某次查询的单价写进脚本或文档，提交前从目标 Workspace / Compute Group 的 Quota 行读取并按实例数计算。

`null`（表里显示 `-`）是「平台没有给这一行定价」，不是免费；两者不能当作同一件事。

### 点券余额有两个，别混

`project list` 给两列，它们是不同的量，成员额度可能远小于项目总余额：

| 列 | `--json` 键 | 含义 |
| --- | --- | --- |
| `My Budget` | `my_remaining_budget` | **当前账号**在这个项目里还能花多少。这是决定你下一个任务能不能起的那个数 |
| `Project Budget` | `project_remaining_budget` | 项目整体还剩多少，所有成员共用 |

平台不给成员额度时两列相同，此时 `My Budget` 是拿项目余额顶上的，不代表平台真的按人分了额度。

`project detail` 再给花在哪儿：`Spent` 拆成 `on training` / `on inference` / `on storage` / `on private workspace`；`Remaining budget` 是同一项目余额的详情投影，不是第二套额度。成员逐人的额度表要 Maintainer 权限，普通成员读到空记录，CLI 不接。

具体可用 GPU 型号、机房和 `gpu,cpu,mem` 三元组仍以当前 Workload 的 Live Quota Row 为准；创建 Workload 时从同一行复制完整 `group` 和 `quota`。提交后再从 Status / Events 核实平台解析出的优先级、排队和抢占结果。

申请资源前按真实任务需求和实时空余选择规格。不要因为猜测主动降档；只有调度语义、空余量或项目策略明确不足时再缩小规模。

## 5. 调度与资源观察

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

工作负载的 Events 只说平台对**这个任务**做了什么，说不了机器本身发生了什么。`resources node-events <节点名>` 是唯一按节点组织的事件源：内核 OOM kill、`TaskHung`、Cordon / Uncordon、重启、`NodeNotSchedulable`。节点名从 `<workload> instances` 的 `Node` 列或 `<workload> status` 拿；可以一次给多个节点，输出多一列 `Node`。命令读取最新 1000 行，`--from` 由平台先按上报组件收窄（`kubelet` / `kernel-monitor` / `node-controller`），`--type` / `--reason` 再在这批最近事件里过滤。**查不到事件不等于机器没问题**，先核对节点名拼写。
