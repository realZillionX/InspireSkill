# Notebook 工作流

创建、连接、执行、传文件，或在 notebook 内准备基底环境时，先查本手册。镜像保存、注册和可见性看 [image-management.md](image-management.md)；workspace、compute group、quota、workload profile 和 path alias 看 [resources-and-paths.md](resources-and-paths.md)；容器内 HTTP 服务暴露看 [notebook-service-proxy.md](notebook-service-proxy.md)。

## 1. Notebook 的角色

Notebook 是交互工作台，不只是“开一个终端”。常见角色：

| 角色 | 用法 |
| --- | --- |
| 联网准备盒 | 在可上网 CPU 组下载代码、依赖、数据和权重，写入共享盘或保存镜像 |
| 训练调试盒 | 在目标 GPU 组做小规模 probe、查看 GPU / CUDA / NCCL / 数据路径 |
| 远端文件入口 | 用 `exec` / `shell` / `scp` 管理共享盘文件 |
| 临时服务盒 | 启动 Gradio、FastAPI、OpenAI-compatible API，再通过 notebook proxy 访问 |

`分布式训练空间` 不可上网时，不要把 `git clone`、外部权重下载或访问公网数据源放到目标 GPU notebook / job 里。先在 `CPU资源空间` 的可上网 CPU notebook 中准备，再把结果留在 `me` / `public` 等共享路径，或保存为镜像。安装 Python / Apt / Conda / npm / Maven 包、访问内部 Docker Harbor 或 OSS 时先看 SII 内部源；它和公网不同，在不可上网 compute group 里也可能可用。

日常 workspace 心智模型很简单：`CPU资源空间` 负责 CPU notebook 和联网准备，`分布式训练空间` 负责 GPU notebook 和训练调试。国产卡分区、`CI-情境智能` 工作空间或其它小组专属空间属于特殊硬件 / 特殊项目路径，只有任务明确要求时才切换。

## 2. CLI Help 查询

Notebook 子命令、参数和默认值以 CLI help 为准：

```bash
inspire notebook --help
inspire notebook create --help
inspire notebook exec --help
inspire notebook scp --help
inspire notebook install-deps --help
```

## 3. 创建前的选择

创建 notebook 前先确认三件事：

1. 准备盒用 `inspire resources specs --usage notebook --workspace CPU资源空间` 选 CPU-only `--quota`；GPU 调试盒用 `inspire resources specs --usage notebook --workspace 分布式训练空间` 选 GPU `--quota`。
2. 确认 `--project <PROJECT>` 是目标项目名。
3. 用 `inspire image list` / `image detail` 选状态可用的镜像。

联网准备盒通常选择 `CPU资源空间` 的可上网 compute group 和 CPU-only quota，例如 `0,20,256`。GPU 调试盒选择 `分布式训练空间` 的 H100 / H200 compute group 和小规模 GPU quota，例如 `1,20,200`。

```bash
inspire notebook create --workspace CPU资源空间 --group CPU资源-2 -q 0,20,256 \
  --name prep-box --image <BASE_IMAGE> --project <PROJECT> --wait

inspire notebook create --workspace 分布式训练空间 --group <GPU_GROUP> -q 1,20,200 \
  --name gpu-probe --image <TRAIN_IMAGE> --project <PROJECT> --wait
```

需要复用同一组条件时，用 `inspire notebook profile set <name> ...` 保存，并在 create 中显式传 `--profile <name>`。

## 4. 连接、`shell` 与 `exec`

先建立 notebook 连接：

```bash
inspire notebook ssh connect <name>
```

`inspire notebook shell <name>` 是持久 SSH 会话，cwd、环境变量和 history 会保留到 `exit`。多个终端并开就是多个独立会话，互相共享同一容器资源。

`inspire notebook exec <name> "<cmd>"` 是一次性独立命令。两次调用之间不共享 cwd 或环境变量。需要连续状态时，把状态放在同一条命令里：

```bash
inspire notebook exec <name> --cwd me:<repo> "export X=1 && ./run.sh"
```

不传 `--cwd` 时，CLI 默认使用 `me` path alias；没有 `me` 时才落到远端 `$HOME`。路径 alias 支持 `me`、`me:<subdir>` 和 `me/<subdir>` 形式：

```bash
inspire notebook exec <name> --cwd me "pwd"
inspire notebook exec <name> --cwd me:<repo> "git pull && pytest -q"
inspire notebook shell <name> --cwd me:<repo>
```

超过 20 分钟的任务写成远端后台进程和 sentinel 文件，再从本机轮询，不要让 `exec` 同步等待。

## 5. 代码、数据和文件流转

| 文件流转类型 | 做法 |
| --- | --- |
| 独立 repo 日常同步 | 本地 `git push`，远端 `git pull` |
| 多仓库工作区 | 通过 `inspire init` 配好 `me`，多个 repo 并列放在 `me:<repo>` |
| 非 Git 文件 | `notebook scp`，远端路径优先写 alias，例如 `me:<repo>/file` |
| `分布式训练空间` 或目标计算组不可上网但共享路径可见 | 在 `CPU资源空间` 的可上网 CPU notebook 做下载 / git / pip，离线训练实例读取共享盘结果 |

`notebook scp` 不是源码同步工具。源码走 `git push` + 远端 `git pull`，否则容易慢且不一致。

常用闭环：

```bash
git push origin <branch>
inspire notebook exec <notebook-name> --cwd me:<repo> "git pull && git log -1 --oneline"
inspire notebook shell <notebook-name> --cwd me:<repo>
```

少量非 Git 文件用 alias 传：

```bash
inspire notebook scp <notebook-name> ./config.yaml me:<repo>/config.yaml
inspire notebook scp <notebook-name> --download me:<repo>/outputs/ ./outputs/ -r
```

## 6. 基底环境与镜像

项目刚开始时，建议在 `CPU资源空间` 用统一基底镜像起一个基底 notebook，把 Slurm、Ray、分布式训练依赖和项目依赖一次性装好。验证通过后保存成项目镜像，后续 notebook、job、HPC、Ray 和 serving 复用该镜像。

```bash
inspire notebook create --workspace CPU资源空间 --group CPU资源-2 -q 0,20,256 \
  --name base-box --image <BASE_IMAGE> --project <PROJECT> --wait

inspire notebook ssh connect base-box
inspire notebook exec base-box --cwd me:<repo> \
  "pip config set global.index-url http://nexus.sii.shaipower.online/repository/pypi/simple && \
   pip config set global.trusted-host nexus.sii.shaipower.online && \
   pip install -r requirements.txt && python -m pytest -q"
inspire notebook install-deps base-box --slurm --ray
inspire image save base-box -n <IMAGE_NAME> -v v1 --public --wait
```

已有 Ubuntu 镜像需要补 Slurm / Ray 依赖时：

```bash
inspire notebook install-deps <name> --slurm --ray
```

该命令会先 probe 再安装，已存在的组件会跳过。普通 notebook 中 Slurm 命令因无 controller 报错是正常现象；只有 `hpc create` 任务运行时才具备完整 Slurm 运行环境。

## 7. 事件、指标和状态

`inspire notebook events <name>` 看调度、镜像拉取、容器启动、停止、保存镜像等生命周期原因。Notebook 卡在 `PENDING`、`CREATING` 或失败时先看 events。

`inspire notebook metrics <name>` 看平台资源视图的历史资源曲线，不需要进入容器。适合判断实例是否真的吃到 GPU、CPU / 内存是否贴边、磁盘或网络是否在持续传输。

常用入口：

```bash
inspire notebook events <name> --tail 50
inspire notebook metrics <name> --window 30m
inspire notebook metrics <name> --metric gpu,gpu_mem,cpu,mem --sparkline --no-plot
```

分工原则：

| 工具 | 主要回答 |
| --- | --- |
| `events` | 平台为什么还没调度、为什么启动失败、生命周期走到哪一步 |
| `metrics` | 资源是否在工作、GPU / CPU / 内存是否打满、I/O 是否还有流量 |
| `exec` / `shell` | 进容器查进程、日志、文件、应用自身状态 |

终态且不再需要的 notebook 要清理；running notebook 先 stop，再 delete。不确定是否仍有人使用时跳过。

## 8. 大文件操作

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

超过 20 分钟的操作一律 `nohup ... &` + sentinel 文件，本地轮询远端 sentinel；不要让 `notebook exec` 同步挂住。并行度不要无脑拉到 64 以上，`-P 16` 通常已经够。
