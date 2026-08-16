# Notebook 工作流

创建交互环境、进入容器、管理远端文件、暴露容器 HTTP 服务，或用 Notebook 准备可复用环境并固化成镜像时看本页。资源条件看 [`resources.md`](resources.md)；联网准备和内部源看 [`internal-sources.md`](internal-sources.md)；保存出来的镜像之后怎么选、怎么共享、怎么清理看 [`image.md`](image.md)。命令语法和参数以 CLI Help 为准。

## 1. Notebook 的角色

Notebook 是交互工作台，不只是“开一个终端”。

| 角色 | 适用场景 |
| --- | --- |
| 联网准备盒 | 在 `CPU资源空间` 准备公网内容，写共享盘或保存镜像 |
| 内部源验证盒 | 在目标 Workspace 验证 `pip` / Apt / Conda / `npm` / Docker 内部源是否可达 |
| GPU Probe | 在 `分布式训练空间` 小规模验证 CUDA、NCCL、数据路径和训练入口 |
| 远端文件入口 | 通过 `shell` / `exec` / `scp` 管理共享盘文件 |
| 临时服务盒 | 跑 Gradio、FastAPI、OpenAI-Compatible API，再通过 Notebook Proxy 访问 |

`分布式训练空间` 不可上网时，不要把外部下载塞进 GPU Notebook 或 Job 的启动路径。公网内容先放到 CPU 准备盒；只依赖 SII 内部源时可以直接在目标 Notebook 验证。

## 2. 创建前判断

创建 Notebook 前只判断平台语义，不在 Reference 里维护完整命令模板：

1. 用真实 Workspace 选择角色：CPU 准备盒走 `CPU资源空间`，GPU Probe 走 `分布式训练空间`。
2. 用 Quota Live 查询选择合法 `gpu,cpu,mem` 三元组。
3. 确认 Project 是目标项目名，Image 已 `READY`。
4. 需要复用同一调度条件时写 Workload Profile；远端目录仍用 Path Alias。
5. 需要数据广场的官方数据集时用 `--dataset <数据集名>:<版本名>`，先按 [`dataset.md`](dataset.md) 确认版本和访问权限。

共享盘默认可写。需要防止误写项目公共目录或项目成员目录时，创建时用只读挂载开关把它们降级为 `ro`；项目成员目录那一档还要求当前账号是项目 Maintainer，否则平台直接拒绝创建。两者默认都不开启，行为与不传时一致。

`--auto-stop` 只表达空闲自动停止请求，不覆盖平台管理员设置的自动回收规则或 Workspace 生命周期上限——那些规则用 `inspire resources policy --workspace <名字>` 读，`分布式训练空间` 对 Notebook 声明的是「GPU 低于 15% 持续 3 小时，或运行超过 18 小时」，所以夜里挂着但不吃卡的 Notebook 第二天不在了是规则生效，不是故障。定时停止是另一回事：它是平台侧的运行时长计时器，到点停机，与空闲无关。长时间训练、批量推理或守护任务应改用 Job、Ray 或 Serving 这类匹配的 Workload。需要在 Notebook 中验证长任务入口时，只跑短 Probe，并把正式命令迁移到后台 Workload。

手动 Pin 节点只用于排查坏节点、复现实验或平台同学明确指定节点；传入所选 Compute Group 中显示的节点名。实际落在哪个节点由 `notebook status` 的 `Node` 给出，后面跟着该节点的健康状态，被 Cordon 或处于维护窗口时一并标出——正在跑不代表下次重启还能落回来。停止的 Notebook 不占节点，这一行随之消失。

## 3. 连接方式

Transport 由机器实际的显卡型号决定：显卡是 `H100` 或 `H200` 的是**受限 Notebook**，不使用 SSH / Rtunnel；其余机器走 SSH。CLI 自动完成该判断——用 JupyterTerminal 在机器上跑一次 `nvidia-smi` 读型号。结果按 **Compute Group** 缓存在 `~/.inspire/notebook-gpu-models.json`：一个组是一池同型号机器，组里第一个 Notebook 探完，之后落在该组的 Notebook 都直接命中。用 `inspire cache clear --resource notebook-gpu` 单独清这一层。

机器答不上来（Notebook 未启动、Jupyter 起不来）时命令直接报错退出，不猜 Transport：`ssh` / `exec` / `shell` / `scp` 本来就都要求 Notebook 处于 `RUNNING`。未启动时提示先 `inspire notebook start <name> --workspace <workspace>`。

| 入口 | 心智模型 | 受限 Notebook 行为 |
| --- | --- | --- |
| `exec` | 一次性独立命令 | 自动走 JupyterTerminal |
| `shell` | 持久交互会话 | 自动走 JupyterTerminal |
| `scp` | SSH / SCP 文件复制 | 受限 Notebook 不使用；改走支持 SSH 的 Notebook 与 `/inspire/...` 共享路径 |
| `ssh` | OpenSSH 交互 | 受限 Notebook 不使用 |
| `ssh-config` | 给 OpenSSH、`scp`、`rsync`、VS Code Remote SSH 使用 | 受限 Notebook 不生成 |
| `connection refresh` | 创建/刷新 SSH / Rtunnel Cache | 受限 Notebook 不建立连接 |
| `ssh-proxy` | OpenSSH ProxyCommand | 受限 Notebook 不使用 |
| `proxy-url` | 返回容器 HTTP 端口的外部地址 | 受限 Notebook 默认拒绝 |

`--workspace` 主要用于首次解析或同名 Notebook 消歧；连接缓存建立后，后续命令通常可按名称使用。缓存是性能和连接复用工具，不是平台事实来源。

受限 Notebook 的 `exec` 每次使用独立临时 Jupyter Terminal，命令结束后立即回收，不共享 `cwd`、环境变量或 Shell 状态。

### 跨账号 Notebook 连接

Notebook 连接类命令包括 `ssh`、`exec`、`shell`、`scp`、`ssh-config` 和 `ssh-proxy`。它们的 `--account <name>` 参数使用本地 Account Alias，也就是 `~/.inspire/accounts/<name>/` 的目录名，不是平台登录 Username。`all` 是跨账号扫描 Selector。

不传 `--account` 时，CLI 会先查 Remembered Target Cache；如果没有可用记录，再扫描所有账号下已有的 Cached Connection。唯一匹配会自动使用；多匹配时会列出候选，交互环境会 Prompt 选择并把选择写入 Target Cache。需要忽略 Remembered Target 时传 `--ignore-target-cache`。

已缓存的 Notebook Connection 不要求当前 Active Account 是 Notebook 所属账号。连接不可用时，CLI 会用目标 Account Alias 对应的 Web Session 和账号配置重建；用户不需要先 `inspire account use <name>`。受限 Notebook 不建立 SSH Connection，命令执行走 JupyterTerminal。

受限 Notebook 的 JupyterTerminal 执行同样复用目标 Account Alias 对应的 Web Session 和代理；显式 `--account <name>` 时不会退回当前 Active Account 的登录态。

刚创建的支持 SSH 的 Notebook 还没有 Cached Connection，`exec` / `shell` / `scp` 会直接报错退出，不会自己 Bootstrap；先跑一次 `inspire notebook connection refresh <name> --workspace <workspace>`。受限 Notebook 不需要这一步，`exec` / `shell` 直接走 JupyterTerminal。`ssh` / `ssh-config` / `ssh-proxy` 自己会建连接，首次仍需要能解析 Notebook 的上下文：通常传 `--workspace <workspace>`，必要时再传 `--account <alias>` 指定所属账号。`ssh-config` 生成的 OpenSSH `ProxyCommand` 会固化解析出的 Account Alias，后续 VS Code Remote SSH / 原生 OpenSSH 连接也按该账号路径执行。

连接缓存由 `notebook connection list/status/refresh/forget/prune` 管理；跨账号 Remembered Target 由 `notebook connection target list/forget` 管理。具体参数以对应 Help 为准。

`exec` 超过 20 分钟时，把任务写成远端后台进程和 Sentinel 文件，再从本机轮询，不要让本机同步等待。

## 4. 路径和文件流转

源码同步优先走 Git：本地 push，远端 pull。`notebook scp` 适合少量非 Git 文件、产物下载和临时配置，不适合作为源码同步主路径。

多仓库项目把 Repo 并列放在 `me:<repo>` 这类路径约定下；项目公共数据、权重和 Checkpoint 放 `public` 或指定存储池 Alias。

跨 Workspace 时先确认共享盘作用域：同项目路径通常可见，不同项目路径通常因 Fileset 隔离不可见。

### 受限 Notebook 文件流转

受限 Notebook 不使用 SSH / SCP / `rsync`。文件流转以共享盘为边界：

1. 在同账号、同项目上下文里选择一个支持 SSH 的 Notebook（显卡不是 `H100` / `H200`，例如 CPU 机器）。
2. 用 `inspire notebook scp <ssh-notebook> ... /inspire/<storage>/...` 上传或下载共享路径文件。需要 `rsync` 语义时，先为该 Notebook 生成 SSH Config，再用外部 `rsync` 操作同一个 `/inspire/...` 路径。
3. 用 `inspire notebook exec <restricted-notebook> "..."` 或 `inspire notebook shell <restricted-notebook>` 在受限 Notebook 内操作同一个 `/inspire/<storage>/...` 路径。

示例：

```bash
# 通过支持 SSH 的 Notebook 从本机上传到共享盘。
inspire notebook scp cpu-box ./dataset.tar /inspire/hdd/project/topic/user/dataset.tar

# 在受限 Notebook 内直接使用同一共享路径，不走 SSH。
inspire notebook exec gpu-box "ls -lh /inspire/hdd/project/topic/user/dataset.tar"

# 通过支持 SSH 的 Notebook 下载共享路径产物。
inspire notebook scp cpu-box -d /inspire/hdd/project/topic/user/results.tar ./results.tar

# 为该 Notebook 配好 SSH 后，也可以用外部 rsync 操作共享路径。
rsync -av ./dataset/ cpu-box:/inspire/hdd/project/topic/user/dataset/
```

受限 Notebook 上 `/inspire/...` 之外的容器本地路径不能由本地 CLI 直接传输；需要带出时先把产物放到 `/inspire/...`。

## 5. IDE URL 与 HTTP Proxy

容器内 HTTP 服务用 `proxy-url` 取得外部地址：

```bash
inspire notebook proxy-url <name> --workspace <workspace> --port 7860
```

它**只打印地址、不打开任何东西**，所以拿到就能直接 `curl`。`--path` 追加服务路径，`--check` 顺带探一次：`reachable` 表示服务在应答，`no_service` 表示端口上没东西（去把服务起起来），`blocked` 表示网关拒绝（权限问题）。

两点必须记住：

- **这个地址本身是凭据。** 它内嵌一段短期 token，拿到的人对该 Notebook 的访问权和你一样。它会进对话记录和 shell 历史，别往外发。
- **Proxy 只提供网络通路，不替代应用自己的鉴权。** Gradio、FastAPI、LLM API 仍要有自己的登录或 API Key。发布给协作者前做无 Key / 有 Key 对照，确认未授权请求会被拒绝。

## 6. 基底环境与保存镜像

项目早期用统一基底镜像起 Notebook，把 Slurm、Ray、分布式训练依赖和项目依赖一次性装好。公网下载放 CPU 准备盒；只缺内部源时可在目标 GPU Notebook 配置验证。

验证通过后保存项目镜像。保存是 **Notebook 的生命周期事件**：`notebook save-image <notebook-name>` 就地提交当前容器，过程中该 Notebook 进入 COMMITTING、不可操作；保存完成后 Notebook 不会自动停止，仍可继续连接和使用，镜像只是这次事件的产物。因此这条命令按 Notebook 名寻址，`--workspace` 指的是 **Notebook 所在的空间**，不是镜像的归属。

保存前平台会给出快照体积估算并先打印出来，用它判断这次会占用多久；`--dry-run` 只看估算、不真的保存。Notebook 未运行时命令直接拒绝，不会产生半成品。

保存跑到一半要拿回 Notebook 时用 `notebook cancel-save-image <notebook-name>`：即使平台已经报告提交完成，取消仍然生效，Notebook 回到保存前的状态。但半成品镜像会以 `FAILED` 留在镜像目录里，需要按 [`image.md`](image.md) 单独删除。

保存出的镜像才是后续 Notebook / Job / HPC / Ray / Serving 应复用的稳定环境。

普通 Notebook 中 Slurm 命令因无 Controller 报错是正常现象；只有 HPC 任务运行时才具备完整 Slurm 运行环境。

## 7. 观察与清理

| 工具 | 主要回答 |
| --- | --- |
| `events` | 平台为什么还没调度、为什么启动失败、生命周期走到哪 |
| `lifecycle` | 每次启动到停止的运行周期 |
| `metrics` | GPU / CPU / 内存 / I/O 是否真的在工作 |
| `exec` / `shell` | 进容器查进程、文件、日志和应用状态 |

Notebook 卡在 `PENDING`、`CREATING` 或启动失败时先看 Events；显示 `RUNNING` 但业务不推进时看 Metrics，再回到应用日志和产物路径。

## 8. 大文件操作

大规模 `mv` / `cp` / `rm` 前先探目录形状：顶层 fan-out、一两个巨型子树、百万级小文件对应的策略不同。

| 形状 | 策略 |
| --- | --- |
| 顶层 fan-out 大且大小均匀 | 顶层并行处理，控制并发 |
| 一两个巨型子树 | 先下钻再并行，否则实际只有一路 |
| 百万级小文件 | 优先使用 `find -delete` 或 `rsync --delete-after` 这类少 fork 的方式 |

超过 20 分钟的操作一律后台运行并写 sentinel；并行度不要无脑拉满，先看文件系统和业务风险。
