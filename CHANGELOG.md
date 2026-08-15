# Changelog

## Unreleased

### 新增

- `inspire dataset list|show|validate`：数据广场（`aip.sii.edu.cn`）的目录、版本和挂载前校验。这是一个和启智并列的独立平台，只共用同一套 CAS SSO，控制台侧边栏的「数据集」就是外链过去的——启智那侧根本没有检索接口，只有一个把某个版本挂进容器的 `dataset.ValidateDataset`。CLI 用现有 Session 里的 CASTGC 走一次 CAS 握手换 `datasets-session`，纯 HTTP，不起浏览器。

  **数据集用名字寻址，不用数字 ID。** 数据广场内部的 `datasetId` / `versionId` 拿去挂载会被拒为「数据集不存在」；认的是 `pixabay-81k` 和 `v0` 这样的 code。`list` 因此把名字当第一列，数字主键只留在 resolver 里；`show` 的每个版本行直接给出可粘贴的 `--dataset` 值和容器内路径。列表还给出 Access 一列：全平台 531 个数据集里有 106 个当前账号无权挂载，不先看这一列就会在创建时才撞上「无访问权限」，而申请权限只有网页端有入口。版本号不保证是 `vN`，`v1-br`、`2026-07-30`、`v3again` 都真实存在，不要按序号猜。

- `notebook create`、`job create`、`hpc create` 支持 `--dataset <数据集名>:<版本名>`，可重复。挂载点固定为 `/inspire/dataset/<数据集名>/<版本名>`，只读，不占项目共享盘配额，也不归 Path Alias 管。创建前平台会逐条校验并解析真实存储路径，任何一条不通就整体失败，不会先建出一个缺数据的 Workload。`ray` 和 `serving` 没有这个选项：平台直接拒绝该字段，网页端对应表单也没有这一项。

- `job create` 支持 `--env KEY=VALUE`（可重复）、`--keep-after-success` / `--keep-after-failure`、`--description`、`--fault-tolerance-retry-interval`。环境变量此前只能拼进启动命令；保留时长让任务结束后容器停在保留态等待，可以直接进去看现场，比重跑一次便宜得多。值可能是凭据，所以 CLI 输出只回显变量名。

- `notebook create` 支持 `--auto-stop-after MINUTES`（平台侧运行时长计时器，与 `--auto-stop` 的空闲判断是两回事）和 `--enable-notification`；三类 Workload 都支持把项目公共目录降级为只读，`notebook` 另有项目成员目录只读一档，后者还要求当前账号是项目 Maintainer，否则平台直接拒绝创建。默认全部保持关闭，不传时的请求体与此前逐字节一致。

- `hpc create` 支持 `--max-time`、`--keep-after-finish`、`--description`，并且 `--enable-notification` 不再被硬编码成关闭（默认值仍是关闭）。

- `inspire ray start`：停掉的 Ray Job 保留完整集群规格，此前 CLI 有 `stop` 却没有 `start`，停了就只能重建。平台在这里会「受理但不执行」——请求返回成功而任务纹丝不动，再调一次变成 `InternalError`，所以命令以状态真的离开 `STOPPED` 为准，没动就报失败。

- `inspire serving scale|versions|rollback|api-metrics`：副本伸缩、部署历史、按历史版本重新部署，以及请求量 / 成功率 / 延迟 / TTFT。`api-metrics` 和既有的 `serving metrics` 是两套不相交的指标——后者看资源占用，只有前者能把「没人调用」和「一直调用一直失败」分开。

- `inspire resources quota`：Workspace 的配额上限、当前占用和集群物理容量并排显示。两者都会拒绝任务但失败形态不同——配额用尽停在 `QUOTA_PENDING`，集群占满停在 `PENDING` 并伴随 `FailedScheduling`，此前 CLI 回答不了「这个规模到底放不放得下」。

- `inspire model deploy-config`：某个模型版本能被部署的最小节点规格，正好是 `serving create --quota` 的下限，同时给出 vLLM 兼容判断。

- `notebook status`、`job status`、`hpc status` 显示实例挂了哪些官方数据集，以及各自在容器里的路径。此前 CLI 能设不能读：建的时候可以 `--dataset`，建完想知道「这里面到底有什么数据」只能回网页看。平台在 `GetNotebook` / `GetJob` 里一直回显这份信息，只是没接。它同时给出容器路径而不是平台内部存储路径——后者命名的是用户既不寻址也用不上的内部布局。

- `inspire job tensorboards`：平台会为训练任务单独跑 TensorBoard，此前 CLI 完全看不见它们。值得做成命令的不是那个网页地址（Agent 没有浏览器，这个 CLI 也早就删掉了「打开一个网页」这类命令），而是 Summary Path——event 文件写在共享盘上的目录，同项目任意 Notebook 直接就能读。于是「平台为这次训练开了 TensorBoard」从一条没法用的信息变成了可执行的路径。

- `inspire dataset tags`：列出 `dataset list --tag` 接受的全部 52 个标签及其所属模态。标签名是固定的中文词（`视频生成`、`具身智能`……），猜不出来，此前唯一的发现路径是故意填错一个再去看报错里的候选。

- `inspire hpc logs`、`inspire serving logs`、`inspire ray logs`：补齐「每种 Workload 都能读到程序输出」这条线上最后三个缺口——此前只有 `job logs`，HPC、Serving 和 Ray 的容器输出在 CLI 里根本看不到。三条命令与 `job logs` 共用同一套记录与字符预算（默认 100 条 / 16,000 字符）和同一份 `--json` schema，实例筛选一律用 `instances` 已经在打印的角色/序号而不是被 `scrub_raw_ids` 洗过的 pod 名。平台侧有三个坑：日志端点的实例名要带命名空间（HPC 裸名报「expect 1, but got 0」，Ray 干脆静默回空）；`page_size=N` 保留的是**最旧**的 N 条，所以「最后 N 条」必须先取满窗口再在客户端截尾；时间窗超一个月平台答 `InternalError`，而这个码在瞬时错误名单里，不在客户端 clamp 就会先白烧三次退避重试再抛出一条看着像平台故障的错。

- `inspire resources usage --workspace <名字> [--by user|project|task] [--mine]`：共享集群上「卡被谁占着」此前从 CLI 完全看不出来——`resources availability` 只回答「还剩多少」。现在按用户、项目或任务报出存活工作负载持有的 GPU / CPU / 内存，以及其中有多少真的在忙（`gpu.used` 是死字段恒 0，`usage_rate` 才是活的）。平台的 `ListProjectDimension` 在 10 个可见工作空间上全部返回空，是权限地板而不是 scoping 写错，所以按项目聚合改在客户端折叠任务维度的行；`--mine` 走 `ListUserDimension`，它只答调用者自己的行。

- `inspire resources policy --workspace <名字>`：报出每类 Workload 的空闲回收规则与运行时长上限。第二天回来发现 Notebook 没了、任务在某个时刻被杀，此前只能猜；这些都是平台明确声明过的配置，只是从来没接进 CLI。AND/OR 条件按原样呈现而不是拍平，Serving 的规则按 GPU 档位逐条给。平台在部分工作空间对 HPC 返回字面量 `null`，渲染成「未声明」而不是「无限制」——那两者是相反的结论。

- `inspire ray scaling` 与 `inspire serving scale-history`：弹性伸缩是 Ray 存在的理由，而「`min_replicas` / `max_replicas` 到底动没动过」此前看不到；Serving 同理，副本数变化是排查延迟突增的第一手材料。两条都做成独立子命令而不是塞进 `status`，因为它们是需要 `--limit` / `--all` 预算的增长型集合。

- `inspire model status` 现在会说出哪些推理服务还占着这个模型版本、整个模型有没有排队中的部署、以及哪些别的版本还在跑。此前从 CLI 完全看不出一个版本有没有人在用，换版本或删模型只能盲操作。只报仍可能起来的服务（`RUNNING` / `STOPPED` / `SLEEPING`），已经失败的不算「有人在用」。

- `inspire notebook save-image` 在发出保存请求之前先报平台估算的快照大小，并新增 `--dry-run` 只估不存；非 RUNNING 的 Notebook 在任何保存请求之前就被拦下。存镜像是一段该 Notebook 不可操作的等待，此前按下去之前完全不知道要等多久、会产出多大的东西。估算失败不阻断保存，只是不打印那一行——取不到的大小读作「未知」，不读作 0。

- `inspire notebook metrics --now`：给出 CPU / 内存 / GPU / 显存的当下快照。此前只有历史时序，而「这个 Notebook 现在到底在不在用卡」需要的是当下的值。

- `inspire hpc events` 支持 `--instance` / `--all-instances` 的实例级事件，并把重复发生的事件折叠进 `Count` 列（job 级也生效）。平台在 HPC 事件上从不填 `count`，而是按发生次数逐行重复：一个失败任务 106 行事件里 `--tail 20` 全是同一条 BackOff，折叠之后 20 行才露出真正的死因。只出现一次的行完全不变。

- `inspire notebook cancel-save-image`：中止进行中的 Notebook 存镜像并立刻把 Notebook 交还。存镜像是一段该 Notebook 不可操作的等待，此前一旦按下去就没有退路。受控验证过两次——保存开始 1 秒时、以及 38 秒后平台已经打出「已提交镜像层，等待推送」之后，**都能成功中止**，Notebook 回到 RUNNING。代价要知道：半成品镜像会以 `FAILED` 留在镜像目录里，得自己删。

- `inspire model delete`：CLI 此前能注册模型却不能删，与仓库自己的清理纪律矛盾。删除前逐版本核对推理服务占用与排队中的部署，有占用就点名拒绝，`--force` 放行；占用探测失败**拒绝删除**而不是当作没占用。

- `inspire dataset applications`：只读查看数据集权限申请与待我审批的条目，状态显示为可读词。提交与审批仍然只在网页端——那两个动作会以你的名义触达真人审批者，CLI 不接。

- `<workload> quota` 新增 `Priority` 列，`job` / `notebook` / `serving` 的 `create` 在提交前拒绝该档位不接受的优先级。平台在工作空间的调度记录里逐档声明 `allowed_priority_levels`——训练区的 1/2/4 卡是 `["low"]`，8 卡满节点不限——此前 CLI 把这四档平等地列出来，用户按 `--priority 4` 提交要等平台拒绝才知道，而拒绝理由看不出与优先级有关。这条知识以前是一段按组名里有没有「训练区」三个字来推断的硬编码提示，现在改成读平台，那段提示已删除。

  三种状态严格区分：`any`（平台声明不限）、`low`（只能低优先级）、`unknown`（菜单没读到）。**读不到不等于不限**，所以 `unknown` 既不显示成 `any`，也不阻断创建——一次平台抖动不该让一个可用的配额看起来不可用。

- `inspire notebook create` 提交前挡住工作空间内的重名。**平台自己不校验重名**——实测用同一个名字连建两个都成功，而重名会让此后每一条 `notebook <动词> <名字>` 都变成歧义、必须 `--pick`。校验是大小写不敏感的，也会忽略尾部空格。探测失败时让路、不拦创建。

- `job / hpc / notebook / serving status` 报出工作负载落在哪些节点，`job / hpc / ray / serving instances` 新增 `Node` 列给出每个 Pod 的落点。此前 CLI 能回答「要了几个节点」却回答不了「是哪几个」——而排查坏节点、复现某次实验、判断掉队的是哪个 Worker，问的都是后者。平台在这些详情里一直回显落点，只是投影层把它当内部字段丢掉了：`train.GetJob` 有 `node_infos[]`（外加请求侧的 `specified_nodes` / `exclude_nodes`，后者正是 `job create --exclude-node` 传进去的那份），`hpc.GetJob` 有 `nodes[]`，`GetServing` 把 `node_names[]` 放在 `extra_info` 里，四个 `ListJobInstances` 族的行都带 pod 级的 `node`。

  **节点名不是平台 handle。** `qb-prod-4090-gpu105` 这类名字是基础设施身份，人和平台同学都按它对话，所以它照常输出；同一行里的 `instance_id` 仍然被洗掉。空的节点清单读作「还没被调度」而不是「查不到」——排队中的任务 `node_count` 有值而落点为空，两者不相等是正常的。

- `notebook status` 的 `Node` 一并给出该节点的健康状态，被 Cordon 或处于维护窗口时标出。**STOPPED 的 Notebook 不会清空节点对象**，而是把名字置空、状态置成 proto 零值 `UNKNOWN_NODE_STATUS`，照直读会印出一个「状态未知的节点」——投影按空名字判定未落点，这一行随之消失。

### 破坏性变更

- `inspire image save` 移到 `inspire notebook save-image`。选项、输出、退出码和 `--json` schema 一个字没变，只换了命令路径，**不保留别名**，所以脚本和 Agent 合同要跟着改。（同组的取消命令是本轮新增的，从未以 `image cancel-save` 发布过，只会以 `notebook cancel-save-image` 出现。）

  归属本来就错了，三处都指向 notebook：这三个动作的平台 Action 全在 notebook 路由上（`SaveNotebookImage` / `EstimateSaveMirrorSize` / `CancelSaveMirror`），`image` 服务下一个都没有；`image` 组其余命令的 NAME 是**镜像名**，而 `save` / `cancel-save` 的 NAME 是 **Notebook 名**——同一个命令组、同一个参数位、两种名词，对 Name-only 的合同是实打实的陷阱；`--workspace` 在这个组里同样有两种含义，`image save --workspace` 指的是 Notebook 所在空间，而镜像 registry 的空间是另一回事。被操作的对象也确实是 Notebook：它在保存期间进入 COMMITTING、不可操作，产物才是镜像。

  没有沿用平台自己的 `CommitNotebook` 叫法：那是控制台的「停止并保存」，镜像名由平台按 `<基底>:stopsave-<notebook>-<hash>-<时间戳>` 自动生成，调用方选不了也猜不到，与这条命令不是一回事。

  `references/notebook.md` 现在承载保存镜像这条动线，`references/image.md` 只留保存之后的可见性与清理并指过去。

### 修复

- **`job events` / `hpc events` 的实例级事件此前混成一条没有署名的时间线。** 行里唯一指明来源的字段是 `object_id`——平台句柄，按设计不进输出——而公共投影把它丢掉了，于是范围一开到 `--all-instances`，拿到的是一堆看不出归属的 `FailedScheduling`，「哪个 Worker 没排上」恰恰在最需要它的场合答不出来。现在按实例查询时输出多一列 `Instance`（`--json` 里是 `instance` 字段）：`hpc` 用 `instances` 打印的角色 / 序号标签，`job` 用实例名。工作负载级事件不带这一列，输出与此前逐字节一致。

- **配额目录此前会丢掉大半计算组，原因是缓存主键漏了组。** 平台把同一档规格在多个组之间**复用同一个 `quota_id`**（分布式训练空间实测：9 个组、11 个不同的 `quota_id`，其中 7 个被 4–7 个组共用），而本地资源索引的主键里没有 `owner_id`，缓存行又是按裸 `quota_id` 存的。于是每个组写入时都覆盖掉前一个组的同名行，只剩最后一个写入者：索引里恰好 11 条 = 11 个不同的 `quota_id`，一条不多一条不少。

  后果是实打实的：`训练区-H200-1号机房` 有 1368 张 H200，v1 也一直正常返回它的四档规格，但 CLI 既列不出也建不了——`--group` 指名道姓同样报「matches no quota row」。修法是让缓存键以计算组打头（真正的 `quota_id` 一直存在行的 payload 里，创建时回显的是那个，不受影响）。修复后索引从 11 条 / 3 组变成 32 条 / 8 组，与平台逐组返回的结果完全一致（第 9 个组 `gpu_total` 真的是 0）。

- `--quota` 匹配不到时，如果同时给了 `--group`，错误不再说「这个 workspace 没有配额」。行集那时已经被 `--group` 收窄过了，那句话把责任推给了错误的作用域；现在按实际作用域说话，并列出该组真实有的档位。

- **私有镜像此前在整个 CLI 里都够不着。** 镜像是按 Workspace 的 registry 存的（每个 `ListImages` / `CreateImage` 都带 `registry_hint`），但读取一侧从来没有把 Workspace 当参数——它从当前 session 上取。这个账号的 session 默认空间恰好是一个空 registry，而 67 个自定义镜像都在另一个空间，于是 `image list` 报「没有镜像」，`job create --image <自己存的镜像>` 报「不在任何目录里」，**「把环境存成镜像再拿去跑任务」这条主动线是断的**。

  修法是把 registry 所在的 Workspace 一路显式传下去：`list_images_by_source` 新增 `workspace_id` 参数，`image list/detail/register/set-visibility/delete` 新增**必填** `--workspace`（语义统一为「镜像 registry 所在的工作空间」），`resolve_image_url`（train / HPC 创建）、serving 与 ray 的镜像解析、以及 `notebook save-image` 存完之后回查镜像，全部改成传入各自已经解析好的目标 Workspace。CLI 层的解析函数把这个参数设成必填而不是留默认值——静默默认正是这个 bug 的成因。有一条测试扫描全仓，任何漏传的调用点都会失败。

- `job logs` / `hpc logs` / `serving logs` / `ray logs` 的 `--json` 不再把日志正文里的绝对路径洗成 `<redacted>`。共享的路径清洗器是为了别让平台句柄漏进输出，但**日志正文是程序自己的话**：`+ /bin/bash -c ...` 被洗成 `+ <redacted> -c ...`、栈回溯的 `File "/opt/conda/.../site.py"` 被洗成 `File "<redacted>"`，正好抹掉这条命令存在的理由。human 输出一直是原样打印的，所以此前同一条日志在两种输出模式下还不一致。清洗只在日志正文字段上豁免，记录里其它字段照旧。

- `inspire ray start` / `ray stop` 不再把平台的状态机拒绝报成 `InternalError`。`ray` 用 `InternalError: RayJob status not allow <动词>` 表达「从这个状态不可能成功」，而这个错误码在瞬时名单里，于是一个永久性的拒绝被读成「平台暂时不舒服」，还会白烧三次退避重试。同时删掉了 `ray start` 那条被实测证伪的提示——它声称没到过 RUNNING 的任务无法重启，而实测这样的任务连续重启了三次。

- `inspire model status` 的 `vllm_ready` 与 `inspire model versions` 的 vLLM 列不再恒为 no。它们读的是版本记录里的存量 `is_vllm_compatible`，而那个字段是死的——29 个可见模型版本上无一为 true，同时两个 live Action 一致地给出 13 个 true。这不只是显示错误：同一个 CLI 里的 `model deploy-config` 一直问的是 live，于是两处对同一个模型给出相反的答案。现在三处都问平台。

- `inspire serving scale-history` 接上时发现 Wrapper 读错了列表键：线上是 `scale_history_items`，而代码读的是 `items` / `list`，于是任何有扩缩容历史的 serving 都会返回空列表。这是「读错键就永远看不到数据」的静默失败，不会报错。（`servings.py` 的模块 docstring 早就写着正确的键名，代码没跟上。）

- 五个 Batch 命令补齐了创建命令这一轮新增的全部字段，此前 Batch 条目严格弱于单条 `create`。`ray batch` 补 `public_path_readonly`，`serving batch` 补 `public_path_readonly` 和 `auto_scaling`；这两类不收数据集挂载，平台直接拒绝该字段，网页端对应表单也没有这一项。`notebook batch` / `job batch` / `hpc batch` 补的是：`dataset`、`env`、`description`、`keep_after_success` / `keep_after_failure`、`fault_tolerance_retry_interval`、`auto_stop_after`、`keep_after_finish`、`max_time`、`enable_notification` 和两档只读挂载都能写进条目了。`dataset` 接受一条 `"<名字>:<版本>"` 或一个列表，`env` 除了 `KEY=VALUE` 列表还接受表——TOML 和 JSON 表达映射比表达拼接字符串自然。数据集在条目准备阶段就完成校验，所以一个拼错的 spec 会在任何东西提交之前中止整个 Batch，而不是等前几条已经跑起来才发现。没有写这些键的条目产生的请求体与此前逐字节一致。

- `hpc status|stop|delete <name>` 不再可能报 `InvalidParameter: page or page_size too large`。名称解析按 `page_size=10000` 请求，而网关上限是 5000。这条上限逐 service 生效——`hpc` 拦，`ray` 给 10000 照收——所以截断放在 `browser_api` 的传输入口无条件生效，而不是逐个 Wrapper 去记：没有哪个调用方应该靠一次失败去学这个上限，而且截断不损失任何东西，超过上限的请求本来也不可能比按上限的请求多返回一行。`page_size: -1`（取全部）平台认，原样放过。

- `hpc list` 的总数不再是「这一页有多少条」。`hpc.ListJobs` 的 `total` 是字符串 `"202"`，而 Wrapper 用 `isinstance(total, int)` 判断后回退到 `len(items)`，于是 202 条任务被报成 100 条。影响不止显示：名称解析的翻页循环以 `已读 >= total` 为终止条件，拿到假的 total 后第一页就停，第 100 条之后的任务按名字根本查不到；`hpc list --all` 的展开分支同样不再触发。`total` 的类型逐 Action 不同，现在统一走一个共享的解析函数。

- `<workload> quota` 不再列出并不受理该 Workload 的计算组。每个组自己声明 `support_job_type_list`，`CPU资源空间` 四个组里只有两个收 `ray_job`，但 `ray quota` 把四个都列了出来，照着选到最后是创建时报「已选择的计算类型组不支持此类型任务」——一条要走到提交才暴露的死路。过滤同时作用于 `quota` 展示、创建时的 `--quota` 解析和配额目录缓存，三条路径用同一个判定。踩到的坑是这个字段是 **JSON 编码的字符串**而不是数组，按数组判断会让过滤看起来生效、实际一条都没滤掉。无法解析该字段时保留该组：读不出来是我们的无知，不是平台的拒绝，而藏掉一个可用的组是更糟的失败——它读起来像「这个空间跑不了这个」，没有任何错误信息会来纠正。

- 平台限流不再被当成「这个 Workspace 没有 Quota」。Quota 目录是每个 Compute Group 一次请求的扇出，`get_resource_prices()` 却把任何失败都 `return []`，于是一次 429 和「这个组确实没有规格」返回同一个值。刷新引擎照单全收：`FetchResult(complete=True)` → `index.reconcile()` → 把上一轮读到的行全部 tombstone，再把空目录标成完整且 fresh。之后 `job quota` / `notebook quota` 报 `No quota rows found.`，`job create` 报 `--quota 1,10,100 matches no quota row`、`Available: (workspace has no quotas)`——同一时刻 Web UI 能在那个组建出 1 卡 Notebook，`resources availability` 也显示还有几十张卡空着。缓存过 TTL 才会自己好，`cache clear` 之后紧接着的另一个进程还可能再踩一次，只有强制完整刷新并真的缓存到行才稳定恢复。（[#68](https://github.com/realZillionX/InspireSkill/issues/68)）

  修在语义上：`get_resource_prices()` 和 `list_notebook_compute_groups()` 不再吞掉请求失败，空列表从此只可能是平台成功回答的空。`fetch_quota_catalog()` 改为返回 `QuotaCatalog(records, complete, error)`——扇出会分片失败，一个组读不到就把这一轮记成 incomplete：读到的行照常写入，读不到的保留旧行，Scope 不算完整刷新，所以要求完整刷新的读者会回到 Live 查询。只有完整的目录才允许 `reconcile()` 去 tombstone。列不出 Compute Group 是另一回事——那时根本没有目录，直接抛错。构建缓存事实时不再接受 `config.toml` 的 Compute Group 兜底：那是给离线显示用的，写进权威缓存会把陈旧的组 ID 变成事实。

  `cache refresh` 因此多一档 `partial` 结果，汇总里报 `N incomplete` 并逐条给出原因，原因同时落到该 Scope 的 `last_error` 上，`cache status` 直接可见，下一次完整刷新才清掉。Quota 解析这一侧也不再把读不到的组当成没有行的组：一个组读不到就抛 `QuotaCatalogUnavailable`，而不是拿剩下的组给答案——那个没读到的组可能正好也有同一个三元组，那是本该要求 `--group` 消歧的情形。`--group` 会在取价格之前先把组过滤掉，所以指定了组的创建不受其他组失败的影响。

- 平台限流统一在传输层退避重试，并作为独立的错误类型抛出。429、408、5xx 和 v2 信封里的 `Throttling` / `ServiceUnavailable` 一类错误码现在抛 `TransientAPIError`（`ValueError` 子类，既有的 `except ValueError` 边界仍然把它映射成 API 错误）。`request_json()` 是 requests 与 Playwright 两条路径唯一的收口，在这里最多重试 3 次，优先采纳平台的 `Retry-After`，否则指数退避加抖动；重试耗尽才让调用方看见。Workspace 级的问题本来就是「每个 Compute Group 一次请求」的扇出，正是限流器会反应的形状，所以这层吸收留在传输层，Wrapper 不各自重试。

- 同一类「一时限流就判定不可用」的结论一并纠正：

  - `resources availability` 不再在某个组被限流时把它整条跳过，也不再把节点维度读不到的组报成 `0` 节点 / `0` 空闲——两者读起来都像是测量结果。现在直接报 API 错误。
  - `job create --image` / `job batch` 的镜像回退不再把「没搜到的镜像源」变成 `Image not found`。
  - `job events` / `hpc events` 不再把过期 Session 和限流一起吞成「没有事件」；平台确实没有事件时仍然返回空。
  - 缓存的名称句柄不会因为 429 被 tombstone：`is_stale_handle_error()` 现在明确排除瞬时状态码，平台没回答不等于句柄失效。
  - Notebook 命令查不到当前账号 ID 时，限流不再被报成 `AuthenticationError`——那会把用户支去重新登录，而实际只需要等一会儿。

### 维护

- Browser API 文档重写。`references/dev/browser-api-v1.md` 和 `browser-api-v2.md` 被删除，替换为三份可独立阅读的参考：[`browser-api.md`](references/dev/browser-api.md)（请求契约、响应信封、认证与 Session、分页、Workspace scoping、错误码、探针方法、仍在用的 v1 端点、回落纪律、输出边界、变更验收）、[`browser-api-actions.md`](references/dev/browser-api-actions.md)（12 条路由 93 个 Action 的请求体、响应键、参数语义、CLI 映射与限制，加上五个创建 Action 的字段合同）、[`data-plaza-api.md`](references/dev/data-plaza-api.md)（数据广场是另一个平台，独立成篇）。

  旧的两份是按迁移顺序累积的工程日志：v1 那份的「当前公开命令映射」有十行只写着「全部已迁 v2」，v2 那份把契约、踩坑记录和迁移复盘混在同一节里，而两份都必须对照才能读懂任何一条。新文档按「维护者要查什么」组织，不再记录迁移过程；已废弃的 v1 域一并移除，只保留四处仍有消费者的 v1 端点及其保留理由。

  随后接入这一批 Action 时，实测又推翻了文档里三处说法：**字段存在性探针会被资源 id 静默废掉**——网关的鉴权中间件在严格 proto 解析之前先读一遍资源键，读不到对象就直接返回，于是 body 里的未知字段根本没被解析，探针给出的「这个字段在合同里」是假的（`ray.GetJobLog` 上四路对照可复现，它让 `ray_job_id` 看起来在合同里，其实不在）；**网关对字段名的大小写和下划线不敏感**，`ImageId` / `image_id` / `Image_Id` 落到同一个字段，所以旧文档那套「同一个标识符三种拼法」的陷阱只剩 `image.UpdateImage` 要裸 `id` 这一处真的存在；**discovery 的响应声明只有最外层的列表键和 total 不可信**，元素内部的属性名是准的。另外把 `workspace.GetScheduleConfig` 归入管理员专用（它与各 Workload 路由下的同名 Action 只是重名），并补上 12 个新接入 Action 的请求体、响应键与限制，Action 总数 93 → 105。

  重写时对着 `browser_api/` 逐个 Wrapper 和一份现取的 `/discovery`（`Version = e1daec0f`，11 个 Service / 175 个 Action）核了一遍，修正了三处旧文档的错误：登录握手仍在用 `/api/v1/user/detail` 和 `/api/v1/user/routes/default`（v2 的 `GetRoutes` 要一个真实 `WorkspaceId`，登录时还没有），此前被漏记成「账号与用户全部已迁 v2」；`workspace.GetWorkspaceQuota` 与 `GetWorkspaceComputeResource` 现在在 discovery 里，此前记的是「两者都不在」；discovery 声明的**响应**结构与线上不符（声明 `Items` / `TotalCount`，实际是 `jobs` / `list` / `logic_compute_groups` 加小写 `total`），此前只说了参数不可信。另外如实标出 15 个有 Wrapper 和测试、但当前没有任何 CLI 命令调用的 Action。

## v7.0.2

### 变更

- Notebook 的 SSH / Rtunnel 可用性改为读机器上真正插的显卡型号，不再看 Compute Group 名称含不含 `H100` / `H200`。名字是人填的标签：可以被改、可以简写，也可以和机器上的硬件对不上，而这条判断决定的是能不能建 Rtunnel。现在用 JupyterTerminal 在机器上跑一次 `nvidia-smi --query-gpu=name`，型号是 `H100` / `H200` 的就是受限 Notebook。

  这不是 v6.3.0 删掉的那条联网探测：那条探的是「能不能上网」，把它当作「能不能 SSH」的代理指标，还要为此起一个完整远端终端竞速四个 TCP 探针（数十秒）。现在探的就是判断本身要的那个事实，一条命令，而且答案会记住。

  记住的粒度是 **Compute Group**，不是 Notebook：一个组就是一池同型号机器，组里第一个 Notebook 探完，之后落在该组的任何 Notebook 都直接命中。组名跟着 Name 解析一起回来（`_resolve_notebook_target` 的第三个返回值、索引里 notebook 记录的 `compute_group`），所以拿这个 Key 不额外发请求；只有平台回包里没有组名时才多一次 Detail 请求，换掉的是该组每个 Notebook 各探一次。结果存 `~/.inspire/notebook-gpu-models.json`，30 天过期纯粹是为了不让文件无限长。`run_notebook_ssh` 里那道内部关卡读同一份，不带 `--workspace` 的 `notebook ssh` 仍然拦得住受限 Notebook 且不重复探测。

  **机器答不上来时直接报错退出，不猜 Transport。** 探测走的就是受限 Transport 那条通道，一台答不上来的机器同样跑不了 JupyterTerminal，而 `ssh` / `exec` / `shell` / `scp` 本来就都要求 Notebook 在 `RUNNING`。此时会去查一次真实状态，未启动就直说未启动并给出 `inspire notebook start <name> --workspace <workspace>`；已经 `RUNNING` 却仍然连不上，就说明是 JupyterTerminal 不通，提示稍后重试或重启 Notebook。

- `inspire cache clear` 和 `inspire cache status` 支持 `--resource <kind>`，与 `cache refresh` 同名同形（可重复，不带即全部）。`clear` 此前只有「全炸」一档：想让一个 Notebook 重新探测显卡，得把 Job、Image、Quota 目录一起赔进去，下一条命令再全部重建；`status` 则只能一次列全部。

  可选的 kind 是 `refresh` 那 15 种，外加 `notebook-gpu`——Notebook 显卡型号那层，不在 Name 解析索引里，但同样是缓存，`status` 和 `clear` 都认它。它进不了 `refresh`：那 15 种背后都有列表接口可以整个 Scope 批量拉，显卡型号只能一个组一个组地开远端终端探，没有「全刷一遍」这个动作。

  `status --resource X` 一定会给 X 一行，没缓存就报 `empty`——问「X 现在什么状态」，沉默不是回答。不带 `--resource` 且整个缓存确实空着时，仍然只打印一句 `Resource name cache is empty.`。`clear` 的人类输出和 `--json` 都报告各清掉多少条；部分清理照样 bump generation，否则一个在途 refresh 会把结果写回刚清空的 Scope。

### 修复

- 打开 Name 解析索引时清掉当前版本已经不认识的资源类型。`ssh-key` 在 v7.0.0 随 `inspire user` 一起删了，但老库里它的 scope 行还在：刷不到（不在刷新注册表里）、点不到（不在 `--resource` 候选里），却一直出现在 `cache status` 里。现在开库时按 `DEFAULT_TTL_SECONDS` 已知的类型扫一遍，孤儿行直接删；索引本来就是可丢弃的加速状态，留着一个谁也读不到的类型没有意义。

## v7.0.1

### 新增

- `inspire uninstall` 卸载自己。三档按归属分，不按省事分：默认删安装器写下、且只有 InspireSkill 要的东西（各 harness 的 skill 目录、macOS 每日检查的 launchd agent 及其日志、`~/.inspire/update-status.json`），最后才是 CLI 包本身；`~/.inspire` 存的是重装后仍然能用的平台凭据，要 `--purge`；Playwright 浏览器缓存装在本机所有 Playwright 工具共读的位置，要 `--purge-runtime`，且不被 `--purge` 蕴含。仓库自己的 `INSPIRE.md` 和 `./.inspire/` 是项目资产，任何一档都不碰。执行前打印完整清单并要求确认（`--yes` 跳过，`--json` 下必须带），路径一律显示成 `~/...`，本机用户名不进输出。

  三处顺序是有讲究的：launchd agent 先 `unload` 再删 plist，否则 launchd 会一直抱怨一个指向不存在二进制的 job；CLI 包最后删，且只在前面每个文件都删干净之后才删——半清理的机器上还得留着那条能把活干完的命令；包删掉之后进程立即 `os._exit`，不做解释器收尾。最后这条是因为卸载会把本进程自己的 venv 抽走：已经载入内存的代码照常跑完（POSIX 会为持有 inode 的进程保留它），但此后任何一次 import 都会落空，而正常的解释器退出路径是会 import 的。所以两种结果的输出都在动包之前就渲染好。

- `scripts/install.sh --uninstall` 做同一件事，参数同名（`--purge` / `--purge-runtime` / `--yes`），供 CLI 已经跑不起来的机器兜底。它在 `curl | bash` 下也能问：此时 stdin 是脚本自己，所以确认提示直接走 `/dev/tty`——并且是真去打开它，不是看 `-r`，因为没有控制终端的会话里那个节点照样存在且可读，只有 open 会失败。

### 修复

- 安装器结尾的 `InspireSkill installed.` 少一个换行，后面的步骤提示会贴在同一行上。

## v7.0.0

### 破坏性变更

- 移除 `inspire notebook url` 和 `inspire notebook vscode`。这个 CLI 由 Agent 驱动，而这两条命令唯一的作用是在本机默认浏览器里打开一个网页——Agent 没有浏览器可开，这个动作对它没有任何意义。要人来看 Web IDE，直接在启智控制台打开 Notebook 即可。

- `inspire notebook proxy-url` 改为**打印**地址，不再打开浏览器。它的用途是拿到 Notebook 容器里某个端口的外部 HTTP 地址，好去请求部署在里面的服务，所以地址本身就是结果；此前它把地址藏起来、只开一个浏览器窗口，对 Agent 完全不可用。human 输出就是一行 URL，`--json` 输出 `{name, url}`（带 `--check` 时多一个 `service_check`）。

  这条命令因此成为整个 Notebook 命令组里唯一打印平台 URL 的命令。**这个地址等同于凭据**：它内嵌一段短期 token，持有者对该 Notebook 的访问权与你相同，而它会进 Agent 对话记录和 shell 历史。没有免 token 的替代形式——平台域上的 `/api/v1/notebook/lab/{id}/proxy/{port}/` 实测 404，只有带 token 的网关 URL 真的会去连容器端口。JSON 输出走新增的显式开关 `format_json(..., preserve_raw={"url"})`，因为默认的句柄清洗会把这条 URL 整条洗成 `<redacted>`，洗完就不通了；这个开关比既有的 `preserve_paths` 更强，只在「洗过就没有意义」的值上使用。

  顺带修正 `--check` 的判定：端口上没有服务时网关返回 500 `connect ECONNREFUSED`，此前落进 `blocked` 这一档，读起来像是权限问题；现在报 `no_service`，与「去把服务起起来」这个真实动作对应。

- 移除 Model Plaza 的全部 Wrapper（`list_model_plaza`、`get_model_plaza_filters`、`get_model_plaza_detail`、`list_model_plaza_related_workspaces`、`get_model_plaza_deploy_serving_config`）。它们从未被任何 `inspire` 命令调用，只存在导出和自测。Model Plaza 是平台侧的公共模型货架（一键部署用），与 `inspire model` 操作的 Model Hub 是两回事——后者是本工作空间自己注册、带版本管理的模型仓库，两者靠 `plaza_publish_status` 和 `model-hub.GetModelPublish*` 相连，那条发布链路仍然保留。随之 `/model_plaza/list` 从「仍留在 v1」清单里消失。

- 移除 `inspire user` 整个命令组。`inspire user permissions` 迁到 `inspire account permissions`，选项、Name-only 输出和 `--limit` / `--all` 边界都不变；`whoami`、`api-keys`、`ssh-keys` 直接删除。这三条各自的理由：`whoami` 只打印姓名和角色，其底层查询作为内部实现仍在（train / hpc / ray / model 列表靠它按当前用户过滤）；API Key 的值只在创建时可见，列出元数据在 CLI 里无法转化为任何操作；`ssh-keys` 管的是平台用户中心的公钥注册表，而 `notebook ssh` / `scp` / `exec` 读的是本机 `~/.ssh/*.pub` 并直接注入，两者互不相干，删除不影响 Notebook SSH。随之移除的还有 `ssh-key` 这一资源索引类型——它存在的唯一目的是让 `ssh-keys delete <name>` 按名字解析。

- 移除 `inspire user quota`。它调用的是 admin-only 端点，普通用户恒定收到 `用户不存在`（账号是存在的，这只是平台拒绝权限的说法），命令自身的 `--help` 和错误提示早已写明这一点。工作空间级配额用 `<workload> quota`，实时占用用 `resources availability`，项目级信息用 `project list`。没有改接 v2 的同名 Action：`workspace.GetDefaultUserQuota` / `GetWorkspaceQuota` 普通成员确实可用，但都是工作空间级的，接过去需要新增必填的 `--workspace`，回答的也不再是「我的账号配额」这个问题。

### 变更

- 恢复 `inspire update` 面向用户的输出：逐步打印进度（检查更新 / 升级 CLI / 刷新 Skill / 校验安装 / 准备浏览器运行时）、列出刷新到的 harness，并打印新旧版本之间的更新摘要（取自 GitHub Releases，回退到 `main` 的 `CHANGELOG.md`）。v6.3.0 把这些一并降级成了 `--debug` 日志，只剩一行 `InspireSkill updated to vX`。诊断细节仍然只进 `--debug`：harness 只报名称不报本地路径，摘要过滤掉安装 / 构建类条目、URL 和绝对路径。`--json` 输出相应新增 `skills` 与 `release_notes` 字段。摘要条目会先合并硬换行的续行，不再从行尾截断成半句话。

- Browser API 按域从 `/api/v1` 迁到 `/api/v2`：notebook、ray、train、hpc、inference_serving、model-hub、project、user、image、file，以及计算组、节点维度、组资源统计和五个 Workload 的 metrics。公开 CLI 合同不变——命令名、参数、Name-only 语义、human 与 JSON 输出都保持原样，写操作全部经过受控验证（在 `CPU资源空间` 起最小规格临时资源跑完整生命周期，train 的删除因为 CPU 组不支持该任务类型，在 `分布式训练空间` 用 1 卡 H100 验证后随即释放；镜像与模型注册各跑了一遍建→读→改→删；「存镜像」用最小 CPU 配额加最小官方镜像起了一个临时 Notebook，真提交出一个 196 MB 的镜像，全部痕迹随即清除）。两代接口的契约差异记在 `references/dev/browser-api.md`。

  第二轮迁移推翻了第一轮的一个前提：**平台的 `/discovery` 清单是不完整的，不能用来否定一个端点有没有对应物。** 第一轮把 `/user/permissions`、`/user/routes`、`/project/list`、`/project/{id}`、`/project/owners`、`/file/*`、`/model_plaza/*`、`/image/create`、`/image/update`、`/model/create` 共 10 个家族判成「没有对应 Action」并保留 v1，依据都是「discovery 里查不到」。逐个实测下来它们全部有可用 Action，只是没被声明——`file` 和 `model_plaza` 连整个路由都不在清单里。判断一个 Action 是否存在只能靠空 body 探针（`InvalidAction` 才是不存在），路由是否存在只能靠 `404` 与 `InvalidAction` 的区别。

  仍留在 v1 的只剩三处，各有实测依据：

  - `/notebook/lab*` 与 Notebook Proxy——反向代理，要转发任意 HTTP 流量，整套 Notebook SSH 也架在它上面。v2 的 Action 模型装不下。
  - `/train_job/remote_cmd`——双向 PTY 流，同理。23 个候选名 × 5 条路由全部 `InvalidAction`。
  - `/resource_prices/logic_compute_groups/`——**不是没有对应物，是换过去更贵**。它一次答完「这个组能选哪些规格」；v2 的 `workspace.GetScheduleConfig` 只给静态菜单，还要按组补 `GetLogicComputeGroupNodeSpecs`（规格得装得进组内机器）和 `GetLogicComputeGroupResource`（组得真有可分配容量）才能筛出同样结果——实测 9 个组从 9 次请求变 19 次，且等于在客户端维护一份平台调度端筛选逻辑的副本。完整规则与逐组验证记在 `references/dev/browser-api.md` 第 8 节。

  平台用户中心的 SSH 公钥接口 `/ssh/*` 不在此列：它随 `inspire user ssh-keys` 一起下线后已无任何消费者，文档里那几行「留在 v1」是残留，一并删除。

### 修复

- `inspire notebook exec` 和 `inspire notebook shell` 走 Jupyter Terminal 时不再启动无头浏览器。那个浏览器只做三件事：取 lab URL、取 `_xsrf`、建/删 terminal。现在分别由 `notebook.GetNotebookAccessUrl`、一次普通 GET（`_xsrf` 本来就是个 cookie）和 `POST`/`DELETE api/terminals` 完成。交互式 shell 的会话本就跑在 Python WebSocket 上；`exec` 的抓取循环从页内 JavaScript 移植到 Python，协议未变（等 prompt、分块喂 stdin、见到 `<marker>:exit:<code>` 收工）。受控验证在 RUNNING 的 CPU Notebook 上完成，全程用 import hook 封死 `playwright` 包，退出码与多行输出都正确。

  顺带说明一个容易误判的事实：`exec` 在该容器上端到端约 31 秒，其中 **27 秒是容器里内层 `bash` 在 source rc 文件**（`build_jupyter_exec_command` 的执行方式一直如此），与传输方式无关，老的浏览器路径同样要付这笔钱。

- Notebook 网关 URL 的解析不再默认起一个无头 Chromium：先问平台的 `notebook.GetNotebookAccessUrl`，拿不到才回落浏览器抓取。两者归一化后的结果**逐字节相同**，耗时 **0.57 秒对 6.4–36 秒**。收口在 `resolve_notebook_vscode_ide_url`，所以 `notebook proxy-url` 和 rtunnel 的 SSH 候选路径同时受益。`--refresh` 也走 API——它的语义是「别信缓存」而不是「一定要抓」；STOPPED 的 Notebook 上 API 返回空串，照旧回落浏览器。

- Notebook 网关 URL 的归一化丢了结尾的斜杠，任何直接打开它的地方都会落到 404。网关对这个 URL 的响应是一个 **302 到相对路径** `./?folder=...`：带斜杠时 `./` 落在 token 目录上，正常加载；不带斜杠时 `./` 被解析到上一级，`<token>` 那段被吃掉，重定向终点就是 404。两种写法对第一个请求都回 302，所以存活探测（只看 2xx/3xx）一直判定「可达」，故障只在跟完重定向后才显现。修复同时会重写磁盘上已缓存的旧地址——那些条目照样探活通过，不主动修就会继续发出坏链接。

  这个 bug 是在删掉 `notebook vscode` 之前发现的，当时正是它打开 404。现存命令里 `proxy-url` 不受影响（它拼 `/proxy/<port>/` 时本来就补了尾斜杠），修复的价值在于归一化结果现在对任何消费方都是可用的。

- `inspire serving start` 与 `inspire serving stop` 此前对任何输入都失败，返回 `API error: None`。这两条命令的 URL 早先已指向 `/api/v2`，但仍用 v1 的信封检查（`code != 0`）解包，而 v2 响应根本没有 `code` 字段，于是每次调用都被判成错误。换用 v2 解包器后暴露出第二个问题：请求体里的 `version` 字段 v2 同样不接受，正确的请求体只有 `{inference_serving_id}`。

- `inspire hpc create` 此前对任何输入都失败，返回 `InternalError: priority must be set`。请求体把优先级写成了 `task_priority`，而 v2 要 `priority`；平台的措辞像是「值没传」而不是「字段名写错」，所以这个问题一直没被认出来。

- `inspire job create` 和 `inspire hpc create` 传镜像显示名（如 `ngc-pytorch:25.02-cuda12.8.0-py3`）会被拒绝，报 `无法找到对应镜像`——平台按 registry URL 匹配，而报错读起来像是镜像不存在。现在显示名会先在镜像目录里解析成 URL；已经是 URL 的直接透传、不查目录，`--image NAME|URL` 的合同不变，刚推送尚未出现在列表里的镜像也仍然可用。

- `inspire hpc list` 的名字列此前恒为 N/A，`inspire hpc status|stop|delete <name>` 也无法按名字定位任何任务：解析读的是 `name`，而平台返回的字段是 `job_name`。

- `inspire ray list` 的 Created By 列此前恒为 N/A：解析读的是 `created_by` 和 `priority`，而平台返回的是 `creator` 和 `priority_name`（前两者始终为 null）。

- `inspire resources nodes` 此前对非工作空间管理员整条命令失败，报 `You are not the admin of any workspace`；同时 `resources availability` 的 Free Nodes 列恒为 0。两者都源于节点数据取自管理员专属端点，而退化路径取到的是硬件规格表、不含实时状态，因此没有任何节点会被判为空闲。现在改用工作空间级的节点维度查询，普通成员可用，空闲节点数是真实统计。

- 并发冷启动的 Notebook SSH 连接不再互相踩踏。VS Code Remote SSH 这类客户端会同时拉起多个 `ssh-proxy` ProxyCommand 进程，此前它们各自看到共享状态缺失或过期，于是同时写 Notebook Target Cache、同时刷新 Web Session、同时跑一遍 Bootstrap，表现为间歇性的平台账号识别失败。现在 Target Cache 的读改写、`web_session.json` 的写入与按账号的登录刷新、以及按账号 / Workspace / Notebook 的冷启动 Bootstrap 都跨进程串行化：等待方复用赢家产出的 Session 与连接，过期的 Session 不再覆盖新的，重新登录后也会回到正常 HTTP 请求路径而不是留在浏览器回退上。等待有上限（Bootstrap 为 `--timeout` 加 60 秒），卡死的持有者不会把后续 `ssh` 永久堵住。

## v6.3.0

### 新增

- `inspire ray metrics` 补齐 Ray 的指标观察，与 job / hpc / serving metrics 同接口。
- `inspire serving events|instances|start` 补齐 Serving 的观察与启动入口。

### 破坏性变更

- 移除 `inspire job id`、`inspire hpc id`、`inspire notebook id`。CLI 不再有任何 Handle 输出入口，用 `list` 拿 Name 即可。
- `inspire notebook ssh open <name>` 改为 `inspire notebook ssh <name>`；`inspire notebook vscode-proxy-suffix` 改为 `inspire notebook vscode`。
- `job|hpc|notebook|serving metrics` 的 `--lcg` 改名为 `--group`，与创建命令一致。
- `serving create --shm-gib` 改名为 `--shm-size`，与 job / notebook / ray 一致。
- `resources nodes` 只保留 `--min-nodes`，去掉同义别名 `--min-free` 与 `--min-full-free-nodes`（三者本就是同一个选项）。
- 移除 `resources availability --no-cache`：它绕过的可用性缓存已整体删除，该命令现在始终读取 Live 数据。
- 移除 `notebook exec` 的 `--artifact-path` / `--download` / `--no-wait` / `--denylist`，以及它们背后的 GitHub Actions bridge action 执行后端和整个 `INSP_GITHUB_*` 配置类别。这条路径需要自建 Actions Runner 挂载集群共享文件系统、在用户仓库里安装 workflow，并把产物经 orphan 分支中转；参考文档从未描述过它，随附示例 workflow 引用的 `inspire bridge exec` 也早已不存在。拉取远端产物请用 `notebook scp -d ... -r`（直连 SSH / SCP，无需绕行 GitHub）；无 tunnel 时的命令执行仍由 Jupyter terminal transport 承担。残留的 `[github]` 配置段会被静默忽略，不影响现有配置加载。
- 移除从 `INSPIRE_USERNAME` / `INSPIRE_PASSWORD` 环境变量读取平台凭据的兜底路径；账号是唯一受支持的凭据来源，用 `inspire account add <name>` 配置。其余 `INSPIRE_*` 配置项（如 `INSPIRE_SHM_SIZE`、`INSPIRE_JOB_ENABLE_NOTIFICATION`）不受影响。
- `inspire update` 不再抓取并打印 GitHub Release 正文。
- 移除 `inspire notebook net-test` 及其背后的 JupyterTerminal 出站探测（`probe_notebook_network` 和相关 Browser API 导出）。它唯一的消费者是 SSH transport 判断，而该判断已改用 Compute Group（见下）。需要确认某个具体端点是否可达时，用 `inspire notebook exec <name> "..."` 在容器里一次性验证。
- `notebook connection list|status` 的 JSON 输出去掉 `public_internet` 字段，本地 bridge profile 也不再持久化 `has_internet`：缓存连接只会存在于支持 SSH 的 Notebook。旧 `bridges.json` 中残留的 `has_internet` 会被静默忽略。

### 变更

- 重构 Agent 使用手册：新增 `references/project-context.md`，承载项目初始化问询（Project / Workspace / Paths / Image 四项须由用户确认，其中 `CPU资源空间` 与 `分布式训练空间` 是默认公共 Workspace，其余专属 Workspace 须用户亲自指认）、`INSPIRE.md` 资产合同（合并原 `project-assets.md`）和项目信息持续维护触发点；`resources-and-paths.md` 更名 `resources.md`、`network-and-sources.md` 更名 `internal-sources.md`、`image-management.md` 更名 `image.md`。删除 SKILL.md 的“网络与合规闸门”一节（此类限制由 CLI 层承担），并清理各使用手册中缓存实现、内部字段名等开发层细节；`references/dev/` 之外的文档只保留面向 Agent 的操作语义。
- Notebook 的 SSH / Rtunnel 可用性改为直接看 Compute Group 名称是否含 `H100` / `H200`，不再靠 JupyterTerminal 联网探测推断。旧路径要为每个非静态受限的 Notebook 开一个完整远端终端跑连通性探测（数十秒），且把“能上网”当作“能 SSH”的代理指标。该判断同时收紧到 `run_notebook_ssh` 内部，因此不带 `--workspace` 的 `notebook ssh` 也无法为受限 Notebook 建立连接。
- Compute Group 名称随 Notebook 一起进本地 Name 解析索引（`resource_identity` 新增 `compute_group` 列，旧库自动 `ALTER TABLE` 迁移，schema 版本 3），由后台定时刷新一并维护。因此 Transport 判断在缓存命中时不发任何 API 请求；缓存未命中时也只从 Name 解析本来就要发的 `/notebook/list` 响应里读，不再额外请求 Notebook Detail。
- 缓存 TTL 整体放宽：Workload（notebook / job / hpc / ray / serving）60 秒 → 5 分钟，平台目录类（workspace / project / compute-group / image / model / ssh-key）5 分钟 → 30 分钟。后台刷新进程的最小间隔取所有 TTL 的最小值，因此每账号的后台刷新从最多 60 秒一个降到最多 5 分钟一个。
- Project 改为按账号全局缓存，不再按 Workspace 分片。一个 Project 可同时归属多个 Workspace（`ProjectInfo.workspace_ids`），平台的 `project/list` 也支持不带 Workspace 过滤，此前每个 Workspace 各存一份、刷新时每个 Workspace 各发一次请求。现在刷新走 `list_all_projects()` 一次拿全。Name 解析的 Scope 由 `scope_workspace_id()` 统一归一化，刷新侧和查询侧不会再对同一个名字用不同的 Scope。
- 新增 Quota 缓存，作为 Name 解析索引的普通成员：一条 Quota 行本身就是 Name → Handle 映射，Name 是 `gpu,cpu,mem` 三元组（正是 `--quota` 传的值），Handle 是平台 `quota_id`。`resource_identity` 新增 `payload` 列存原始规格对象（`create` 需要回传 `cpu_type` / `gpu_type`），旧库自动 `ALTER TABLE` 迁移，schema 版本 4。Resource 名为 `quota-notebook` / `quota-job` / `quota-hpc` / `quota-ray` / `quota-serving`，Scope 是 Workspace，TTL 30 分钟，因此 `cache refresh --resource quota-notebook`、`cache status`、`cache clear` 和后台定时刷新全部自动覆盖，Admin 删掉的规格也会被 reconcile 正常 tombstone。此前 `<workload> quota` 查询和 `create --quota` 解析都要对 Workspace 里的每个 Compute Group 各发一次规格请求（1 + N），Scope 新鲜时现在是 0 次。

- 新增按账号隔离、可定时刷新且支持手动管理的本地 Name 解析索引；`inspire cache status|refresh|clear` 用于查看、刷新或清理该加速层，平台 Live API 仍是资源事实源。
- CLI 的公共输入、Help、错误、人类输出和 JSON 输出保持严格 Name-only；平台 Handle 只存在于内部解析器、Browser API 请求和本地调试实现中。
- 发现类列表默认限制为 20 项，Batch 结果和 Job 日志使用明确的输出预算；使用 `--limit/-n` / `--all` 主动调整集合输出。
- 同类 workload 命令统一使用相同的 Name、Workspace、分页、确认、截断和 JSON 语义。

### 修复

- Handle 识别只覆盖平台真正会签发的前缀。`node-`、`task-`、`pod-`、`instance-`、`container-`、`group-`、`cg-`、`compute-group-`、`proj-`、`workspace-` 以及 `lcg-1` 这类短数字后缀恢复为普通 Name：此前它们在 list / JSON 输出里显示为 `<redacted>`，同时在输入侧被拒绝，导致这样命名的资源在 Name-only CLI 中完全无法访问。
- 交互式与远端字节流不再改写。`job shell`、`notebook shell`、`notebook exec` 和 Jupyter 终端此前会扣住每个 chunk 结尾的疑似 Handle 片段，造成 raw 模式下无按键回显、提示符残缺、全屏程序刷新不全。日志正文同样按原样输出。
- 带标签的 UUID 完整打码，不再只截掉首段而留下其余部分。
- `_post-update` 重新接受并忽略 `--previous-version`：v6.2.0 及更早版本在自更新时一定会传该参数，缺少它会让升级在交接处失败，跳过 Skill 刷新与运行时安装。
- `notebook ssh-proxy` 重新接受并忽略 `--quiet`；`notebook ssh-config` 生成的 ProxyCommand 恢复使用 `inspire` 的绝对路径（OpenSSH 经 `/bin/sh` 执行，PATH 与交互 Shell 不同），且 `$HOME` 之外的 IdentityFile 不再被静默丢弃。

### 维护

- 开发依赖只保留 `[dependency-groups] dev`，移除 `[project.optional-dependencies] dev` 与 black 配置：用 `uv sync --dev` 装开发环境，`pip install -e ".[dev]"` 不再可用。
- 移除 commitizen 配置。发版时需手动同步 `cli/inspire/__init__.py` 的 `__version__` 与 `cli/pyproject.toml` 的 `version`，并打 `v<version>` tag 触发 publish workflow——原先由 `[tool.commitizen] version_files` 保证的两处一致性现在没有工具兜底。

## v6.2.0

### 新增

- GPU Job 支持平台原生状态通知，可通过 CLI、账号配置或 Batch item 配置开关。
- 支持 Qoder Work 和 Kimi Desktop Harness。

### 变更

- 代理、登录诊断和 JSON 输出统一脱敏，不输出凭据、内部路径、请求包装或平台 Handle。
- Notebook、GPU Job、HPC、Ray、Serving、Image、Model、Project、Resources 和 User 命令统一采用短、可读、可脚本消费的输出。

### 修复

- 修复通用 Shell proxy 与 `NO_PROXY` 的继承边界。
- 修复受限 Notebook 的内部镜像访问不应继承容器代理的问题。
- 修复 Job 通知、容错默认值和项目优先级在分层配置与 Batch 路径中的传递。

## 历史版本

请参阅 [GitHub Releases](https://github.com/realZillionX/InspireSkill/releases)。
