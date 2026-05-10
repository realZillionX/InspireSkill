# Notebook 工作流

创建、连接、执行、传文件，或在 notebook 内准备基底环境时，先查本手册。镜像保存、注册和可见性看 [image-management.md](image-management.md)；workspace、compute group、quota、workload profile 和 path alias 概念看 [resources-and-paths.md](resources-and-paths.md)。如果任务是把容器内 HTTP 服务暴露给浏览器或 SDK，单独看 [notebook-service-proxy.md](notebook-service-proxy.md)。

## 1. CLI help 查询

notebook 子命令、参数和功能说明以 CLI help 为准，不在手册里维护速查表。

```bash
inspire notebook --help
inspire notebook create --help
inspire notebook exec --help
inspire notebook scp --help
```

需要确认单个操作时，先查对应子命令的 `--help`，再结合本文档的约束选择 workspace、compute group、路径和执行方式。

## 2. `shell` 与 `exec`

`inspire notebook shell <name>` 是持久 SSH 会话，cwd、环境变量和 history 会保留到 `exit`。多个终端并开就是多个独立会话，互相共享同一容器资源。

`inspire notebook exec <name> "<cmd>"` 是一次性独立子进程。两次调用之间不共享 cwd 或环境变量。需要连续状态时，把状态放在同一条命令里：

```bash
inspire notebook exec <name> --cwd me:<repo> "export X=1 && ./run.sh"
```

不传 `--cwd` 时，CLI 默认使用 `me` path alias；没有 `me` 时才落到远端 `$HOME`。路径 alias 支持 `me`、`me:<subdir>` 和 `me/<subdir>` 形式。需要长期使用的子目录可以先登记成专用 alias：

```bash
inspire notebook set-path <name> /inspire/ssd/project/<topic>/<user>/<repo> as repo
inspire notebook exec <name> --cwd repo "pwd"
```

超过 20 分钟的任务写成远端后台进程和 sentinel 文件，再从本机轮询，不要让 `exec` 同步等待。

## 3. 连接命令边界

`inspire notebook ssh <name>` 建立并缓存连接；后续 `exec`、`shell` 和 `scp` 都使用 notebook name，不需要在任务中处理连接细节。

```bash
inspire notebook ssh <name>
inspire notebook connections
inspire notebook exec <name> --cwd me "hostname"
inspire notebook scp <name> ./config.yaml me:<repo>/config.yaml
```

Agent 使用 notebook 时只需要把 CLI 当作稳定工具：按 name 调用、读取 human 输出、根据任务需要进入容器或传文件。连接建立与缓存格式属于 CLI 内部实现，不是日常任务的决策材料。

冷启动时间很贵时，可以 `image save` 派生镜像固化环境；一次性任务用完即弃即可。

## 4. 事件与指标观察

`inspire notebook events <name>` 看调度、镜像拉取、容器启动、停止、保存镜像等生命周期原因。notebook 卡在 `PENDING`、`CREATING` 或失败时先看 events。

`inspire notebook metrics <name>` 看平台 `资源视图` 的历史资源曲线，不需要进入容器。适合判断实例是否真的吃到 GPU、CPU / 内存是否贴边、磁盘或网络是否在持续传输。

常用入口：

```bash
inspire notebook events <name> --tail 50
inspire notebook metrics <name> --window 30m
inspire notebook metrics <name> --metric gpu,gpu_mem,cpu,mem --sparkline --no-plot
```

默认 `metrics` 查询 `core` 指标，即 GPU 使用率、GPU 显存、CPU 和内存；`--metric all` 会加上磁盘读写和网络读写。需要给人看趋势时保留默认 PNG 图；只想在终端快速判断时用 `--no-plot --sparkline`。

分工原则：

| 工具 | 主要回答 |
| --- | --- |
| `events` | 平台为什么还没调度、为什么启动失败、生命周期走到哪一步 |
| `metrics` | 资源是否在工作、GPU / CPU / 内存是否打满、I/O 是否还有流量 |
| `exec` / `ssh` | 进容器查进程、日志、文件、应用自身状态 |

终态且不再需要的 notebook 要清理；running notebook 先 stop，再 delete。不确定是否仍有人使用时跳过。实例启动失败或长时间卡住时先看 `events`，不要凭猜测重复创建同规格实例。

## 5. 代码与文件流转

| 文件流转类型 | 做法 |
| --- | --- |
| 独立 repo 日常同步 | 本地 `git push`，远端 `git pull` |
| 多仓库工作区 | 通过 `inspire init` 配好 `me`，多个 repo 并列放在 `me:<repo>` |
| 非 Git 文件 | `notebook scp`，远端路径优先写 path alias，例如 `me:<repo>/file` |
| 目标计算组不可上网但共享路径可见 | 在同一路径的可上网区 notebook 做 git 操作，离线训练实例只读共享盘结果 |

`notebook scp` 不是源码同步工具。源码走 `git push` + 远端 `git pull`，否则容易慢且不一致。

日常闭环：

```bash
git push origin <branch>
inspire notebook exec <notebook-name> --cwd me:<repo> "git pull && git log -1 --oneline"
inspire notebook ssh <notebook-name> --cwd me:<repo>
inspire notebook exec <notebook-name> --cwd me "hostname"
```

少量非 Git 文件用 alias 传，不要回到绝对路径：

```bash
inspire notebook scp <notebook-name> ./config.yaml me:<repo>/config.yaml
inspire notebook scp <notebook-name> --download me:<repo>/outputs/ ./outputs/ -r
```

大规模 `mv` / `cp` / `rm` 前先探形状：

```bash
ls -A <dir> | wc -l
du -sh --max-depth=1 <dir>
```

按形状选策略：

| 形状 | 策略 |
| --- | --- |
| 顶层 fan-out 大且大小均匀 | `find <root> -mindepth 1 -maxdepth 1 -print0 \| xargs -0 -n 1 -P 16 rm -rf --` |
| 一两个巨型子树 | 下钻一两层再 fan-out，否则并行度实际只有 1 路 |
| 百万级小文件 | 优先 GNU `find -delete` 或 `rsync --delete-after empty/ target/`，减少 fork 和 metadata 压力 |

超过 20 分钟的操作一律 `nohup ... &` + sentinel 文件，本地轮询远端 sentinel；不要让 `notebook exec` 同步挂住。并行度不要无脑拉到 64 以上，GPFS metadata server 是共享资源，`-P 16` 通常已经够。

## 6. 基底 notebook 与镜像

项目刚开始时，建议在可上网 CPU 空间用统一基底镜像起一个基底 notebook，把 Slurm、Ray、分布式训练依赖和项目依赖一次性装好。本文只描述 notebook 内部准备和验证；固化为镜像、写入对应 workload profile 和可见性由 [image-management.md](image-management.md) 负责。

> 下述示例中的 `<GROUP>`、`<WORKSPACE>`、`<IMAGE_URL>` 仅为占位格式。实际值以 `inspire resources specs` 和 `inspire config context` 的实时输出为准。

```bash
inspire notebook create --workspace <WORKSPACE> --group <GROUP> -q 0,20,256 \
  --name cpu-box --image <IMAGE_URL> \
  --project <P> --wait

inspire notebook ssh cpu-box
inspire notebook exec cpu-box "apt-get update && apt-get install -y <deps> && pip install <pkgs>"
```

依赖验证通过后，转到 [image-management.md](image-management.md) 执行 `image save`、确认 `READY`，再按项目需要把镜像名写入对应 workload profile，或在创建命令中显式传 `--image`。

已有 Ubuntu 镜像需要补 Slurm/Ray 依赖时：

```bash
inspire notebook install-deps <name> --slurm --ray
```

该命令会先 probe 再安装，已存在的组件会跳过。普通 notebook 中 Slurm 命令因无 controller 报错是平台设计，只有 `hpc create` 路径下才注入 controller。
