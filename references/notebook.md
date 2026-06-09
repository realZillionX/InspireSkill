# Notebook 工作流

创建交互环境、进入容器、管理远端文件、暴露容器 HTTP 服务，或用 notebook 准备可复用环境时看本页。资源条件看 [resources-and-paths.md](resources-and-paths.md)；公网和内部源看 [network-and-sources.md](network-and-sources.md)；镜像生命周期看 [image-management.md](image-management.md)。命令语法和参数以 CLI help 为准。

## 1. Notebook 的角色

Notebook 是交互工作台，不只是“开一个终端”。

| 角色 | 适用场景 |
| --- | --- |
| 联网准备盒 | 在 `CPU资源空间` 准备公网内容，写共享盘或保存镜像 |
| 内部源验证盒 | 在目标 workspace 验证 pip / apt / conda / npm / Docker 内部源是否可达 |
| GPU probe | 在 `分布式训练空间` 小规模验证 CUDA、NCCL、数据路径和训练入口 |
| 远端文件入口 | 通过 shell / exec / scp 管理共享盘文件 |
| 临时服务盒 | 跑 Gradio、FastAPI、OpenAI-compatible API，再通过 notebook proxy 访问 |

`分布式训练空间` 不可上网时，不要把外部下载塞进 GPU notebook 或 job 的启动路径。公网内容先放到 CPU 准备盒；只依赖 SII 内部源时可以直接在目标 notebook 验证。

## 2. 创建前判断

创建 notebook 前只判断平台语义，不在 reference 里维护完整命令模板：

1. 用真实 workspace 选择角色：CPU 准备盒走 `CPU资源空间`，GPU probe 走 `分布式训练空间`。
2. 用 quota live 查询选择合法 `gpu,cpu,mem` 三元组。
3. 确认 project 是目标项目名，image 已 `READY`。
4. 需要复用同一调度条件时写 workload profile；远端目录仍用 path alias。

手动 pin 节点只用于排查坏节点、复现实验或平台同学明确指定节点。节点名是 compute group 里的节点名，不是平台 handle；节点必须属于所选 group。

## 3. 连接方式

| 入口 | 心智模型 |
| --- | --- |
| `ssh` | 交互 SSH；也可接一次性远程命令 |
| `shell` | 持久会话，cwd、环境变量和 history 留在会话内 |
| `exec` | 一次性独立命令，两次调用不共享 cwd 或环境 |
| `ssh-config` | 给 OpenSSH、scp、rsync、VS Code Remote SSH 使用 |
| `connection` | 管理 SSH / rtunnel 连接缓存 |

`--workspace` 主要用于首次解析或同名 notebook 消歧；连接缓存建立后，后续命令通常可按名称使用。缓存是性能和连接复用工具，不是平台事实来源。

### 跨账号 Notebook 连接

Notebook 连接类命令包括 `ssh`、`exec`、`shell`、`scp`、`ssh-config` 和 `ssh-proxy`。它们的 `--account <name>` 参数使用本地 account alias，也就是 `~/.inspire/accounts/<name>/` 的目录名，不是平台登录 username。`all` 是跨账号扫描 selector。

不传 `--account` 时，CLI 会先查 remembered target cache；如果没有可用记录，再扫描所有账号下已有的 cached bridge。唯一匹配会自动使用；多匹配时会列出候选，交互环境会 prompt 选择并把选择写入 target cache。需要忽略 remembered target 时传 `--ignore-target-cache`。

已缓存的 notebook connection 不要求当前 active account 是 notebook 所属账号。SSH tunnel 不可用、需要 rebuild 时，CLI 会自动用目标 account alias 对应的 Web session、账号配置、Playwright proxy 和 rtunnel state 重建；用户不需要先 `inspire account use <name>`。

没有任何 cached connection 时，首次 bootstrap 仍需要能解析 notebook 的上下文：通常传 `--workspace <workspace>`，必要时再传 `--account <alias>` 指定所属账号。`ssh-config` 生成的 OpenSSH `ProxyCommand` 会固化解析出的 account alias，后续 VS Code Remote SSH / 原生 OpenSSH 连接也按该账号路径执行。

`exec` 超过 20 分钟时，把任务写成远端后台进程和 sentinel 文件，再从本机轮询，不要让本机同步等待。

## 4. 路径和文件流转

源码同步优先走 Git：本地 push，远端 pull。`notebook scp` 适合少量非 Git 文件、产物下载和临时配置，不适合作为源码同步主路径。

多仓库项目把 repo 并列放在 `me:<repo>` 这类路径约定下；项目公共数据、权重和 checkpoint 放 `public` 或指定存储池 alias。路径语义写进 `INSPIRE.md`，不要散落在本地 agent 计划文件里。

跨 workspace 时先确认共享盘作用域：同项目路径通常可见，不同项目路径通常因 fileset 隔离不可见。

## 5. IDE URL 与 HTTP Proxy

Notebook Web IDE URL 是浏览器入口，受启智登录态和项目权限约束，不是 SDK base URL。

容器内 HTTP 服务用 notebook proxy 暴露。Proxy 只提供网络通路，不替代应用自己的鉴权；Gradio、FastAPI、LLM API 仍要有自己的登录或 API key。发布给协作者前做无 key / 有 key 对照，确认未授权请求会被拒绝。

不要用本机临时 gateway 绑定 `0.0.0.0` 对外分享，这会绕开启智访问控制。

## 6. 基底环境

项目早期用统一基底镜像起 notebook，把 Slurm、Ray、分布式训练依赖和项目依赖一次性装好。公网下载放 CPU 准备盒；只缺内部源时可在目标 GPU notebook 配置验证。

验证通过后保存项目镜像。`image save` 会触发中等时长的保存过程，期间不可操作该 notebook；保存完成后 notebook 不会自动停止。保存出的镜像才是后续 notebook / job / HPC / Ray / serving 应复用的稳定环境。

普通 notebook 中 Slurm 命令因无 controller 报错是正常现象；只有 HPC 任务运行时才具备完整 Slurm 运行环境。

## 7. 观察与清理

| 工具 | 主要回答 |
| --- | --- |
| `events` | 平台为什么还没调度、为什么启动失败、生命周期走到哪 |
| `metrics` | GPU / CPU / 内存 / I/O 是否真的在工作 |
| `exec` / `shell` | 进容器查进程、文件、日志和应用状态 |

Notebook 卡在 `PENDING`、`CREATING` 或启动失败时先看 events；显示 `RUNNING` 但业务不推进时看 metrics，再回到应用日志和产物路径。

终态且不再需要的 notebook 要清理。Running notebook 先 stop，再 delete；不确定是否仍有人使用时跳过。

## 8. 大文件操作

大规模 `mv` / `cp` / `rm` 前先探目录形状：顶层 fan-out、一两个巨型子树、百万级小文件对应的策略不同。

| 形状 | 策略 |
| --- | --- |
| 顶层 fan-out 大且大小均匀 | 顶层并行处理，控制并发 |
| 一两个巨型子树 | 先下钻再并行，否则实际只有一路 |
| 百万级小文件 | 优先使用 `find -delete` 或 `rsync --delete-after` 这类少 fork 的方式 |

超过 20 分钟的操作一律后台运行并写 sentinel；并行度不要无脑拉满，先看文件系统和业务风险。
