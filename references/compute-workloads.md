# Job、HPC 与 Ray

提交 GPU job、CPU HPC、Ray 和 serving，或观察事件、日志和指标时，先查本手册。这里覆盖运行模型、优先级、事件、指标和状态判断；资源目录、workload profile、path alias、项目配额看 [resources-and-paths.md](resources-and-paths.md)，镜像来源、保存、注册和可见性看 [image-management.md](image-management.md)。

> 下述示例中的 `<GROUP>`、`<WORKSPACE>`、`<IMAGE_URL>` 仅为占位格式。实际值以 `inspire resources specs` 和 `inspire config context` 的实时输出为准。

## 1. GPU 多节点任务：`job`

`inspire job` 覆盖 GPU 多节点工作负载，包括分布式训练、批量推理和并发单节点 worker pool。`job` 是 GPU 路径；`hpc` 是 CPU Slurm 路径。

命令列表、参数和单命令功能以 CLI help 为准。先用 `inspire job --help` 看可用子命令；需要提交、查看、日志、事件、停止、删除或指标时，再分别查 `inspire job <subcommand> --help`。

`job list`、状态判断和 events 诊断都应使用平台实时结果，不把本地历史 cache 当事实来源。

配置了 `me` path alias 时，`job create` 会在远端先进入 `me` 根目录，并把 stdout/stderr 捕获到 `me/.inspire/` 供 `job logs` 查询。训练 repo 建议放在 `me:<repo>`；job 命令里写相对 `me` 的路径：

```bash
inspire job create -n <name>-train -q 8,160,1800 --nodes 2 \
  -c 'bash <repo>/train.sh' --workspace <WORKSPACE> --group <GROUP> \
  --image <IMAGE_URL> --priority 5
```

`job create` 本身不解析 `me:<repo>`；alias 解析发生在 CLI 参数层，例如 `notebook exec --cwd me:<repo>` 和 `notebook scp ... me:<repo>/file`。提交后需要跟日志时运行：

```bash
inspire job logs --follow <name>-train
```

## 2. 优先级

`--priority` 是 1 到 10 的数字，平台映射为三档：

| 数值 | 平台语义 |
| --- | --- |
| 1 到 3 | 低优先级，会被高优任务抢占 |
| 4 | 普通优先级 |
| 5 到 10 | 高优先级，适合稳定训练 |

需要稳定运行时传 5 或更高。提交后用人类输出核对：

```bash
inspire job status <name>
```

如果显示为 LOW，先 stop，再用更高优先级重提。

## 3. HPC 两层资源模型

`hpc create` 有两层资源，不能混：

| 层级 | 参数 | 含义 |
| --- | --- | --- |
| 节点级 | `--quota gpu,cpu,mem` 和 `--instance-count` | 选择平台计算资源规格，以及申请多少个这样的节点 |
| Slurm 级 | `--number-of-tasks`、`--cpus-per-task`、`--memory-per-cpu` | 告诉 Slurm 如何在节点配额内切任务 |

不传 Slurm 级参数时，默认 `cpus-per-task = quota.cpu`、`memory-per-cpu = quota.mem // quota.cpu`、`number-of-tasks = 1`，即整节点一个 task。

HPC 关键约束：

1. `-c` 只写 Slurm 正文，平台自动补 `#SBATCH` 头；程序必须显式 `srun` 启动。
2. `--group "<name>"` 按 name 传。
3. Slurm 级参数超出节点规格时可能静默排队。
4. `--image` 必须是完整 Docker 地址，并带可用 Slurm 环境。
5. 平台自身吃约 0.3 核 CPU 和 384 MB 内存，应用层并发压到 `cpus-per-task - 4` 或更低。
6. 并非所有 CPU compute group 都支持 `inspire hpc create`。提交前用 `inspire resources specs --usage hpc` 确认目标组可用。某些组的大内存规格可能静默排队；真需要大内存交互处理时，退化成在可上网组开 notebook 做交互。

示例：

```bash
inspire hpc create -n <name>-preprocess \
  -c 'srun bash -lc "python preprocess.py"' \
  --group <GROUP> --workspace <WORKSPACE> \
  -q 0,20,256 \
  --cpus-per-task 16 --memory-per-cpu 12 \
  --number-of-tasks 1 --instance-count 1 \
  --project <P> --image <IMAGE_URL> \
  --image-type SOURCE_PRIVATE
```

平台自动注入类似以下 Slurm 头，不要在正文重复维护：

```bash
#SBATCH -o /hpc_logs/slurm-%j.out
#SBATCH -e /hpc_logs/slurm-%j.err
#SBATCH --ntasks=*
#SBATCH --cpus-per-task=*
#SBATCH --mem=*G
#SBATCH --time=*
```

`status=SUCCEEDED` 不等于业务逻辑真跑过。Slurm 作业状态只反映调度器分配和进程退出码，不校验业务产出。每个新 entrypoint 写唯一 fingerprint 到共享盘，再用同项目 notebook 回读确认产出完整。

## 4. Ray 适用边界

默认不要使用 Ray，除非任务明确需要弹性 worker、长守护、流式处理或异构 worker。固定规模 GPU 走 `job`，固定规模 CPU 走 `hpc`。

Ray 当前只在部分 workspace 和 compute group 可用，整体仍偏试验性。使用前先查：

```bash
inspire resources specs --usage ray
```

示例：

```bash
inspire ray create -n <name>-pipeline \
  -c 'python driver.py --mode run_and_exit' \
  --head-image <IMAGE_URL> \
  --head-group <GROUP> --head-quota 0,2,8 \
  --worker 'name=w1;image=<IMAGE_URL>;group=<GROUP>;quota=0,4,16;min=1;max=8;shm=32' \
  -p <P> --workspace <WORKSPACE>
```

Ray 特有坑：

- 镜像必须带 Ray runtime。
- `--head-quota` 和 worker `quota=` 用 Ray 专属配额表。
- `min` 和 `max` 都必须大于等于 1。
- driver 不退出，集群就一直占配额；长守护任务要接受手动 stop 的运维模型。

## 5. 事件与指标观察

任务卡住或失败时优先查事件，确认调度器、控制器或 kubelet 给出的原因：

```bash
inspire job events <name> --tail 50
inspire hpc events <name> --tail 50
inspire ray events <name> --tail 50
```

需要看实际 pod / component 列表时查 instances，并显式传 workspace：

```bash
inspire job instances <name> --workspace <WORKSPACE>
inspire hpc instances <name> --workspace <WORKSPACE>
inspire ray instances <name> --workspace <WORKSPACE>
```

`job events` 支持 pod/instance 级事件过滤；HPC 和 Ray 的 events 命令保持 job-level 事件观察，具体实例清单走对应 `instances` 命令。

任务已启动但健康度不明时查指标。`metrics` 对应平台 `资源视图`，适合看 GPU、显存、CPU、内存、磁盘和网络是否持续工作，以及多 pod / 多 task 是否负载均衡：

```bash
inspire job metrics <name> --window 30m
inspire job metrics <name> --metric gpu,gpu_mem,cpu,mem --sparkline --no-plot
inspire hpc metrics <name> --metric cpu,mem,disk_read,disk_write --window 2h
```

默认 `--metric core` 查询 GPU 使用率、GPU 显存、CPU 和内存；`--metric all` 会加磁盘读写和网络读写。多节点训练重点看每个 pod 的 GPU 和网络曲线是否同步：某个 worker 长期低 GPU、低网络，通常比单条日志更早暴露数据加载、通信或进程卡死问题。CPU HPC 重点看 CPU、内存和磁盘读写；Slurm 显示 `RUNNING` 但指标长期为零时，应回到日志和产出文件确认程序是否真的启动。

Ray 任务以 `ray events`、`ray status` 和日志作为观察面；metrics 入口用于 notebook、job、HPC 和 serving。

| 工具 | 主要回答 |
| --- | --- |
| `events` | 为什么排队、为什么启动失败、调度器拒绝了什么 |
| `metrics` | 已启动任务是否仍在有效工作、各 pod / task 是否均衡 |
| `logs` | 程序自身报错、训练进度、业务输出 |
| `status` | 平台状态、优先级、实例列表和基础摘要 |

终态且不再需要的 job、HPC、Ray 或 serving 要清理。running 资源先 stop，再 delete；不确定是否仍有人使用时跳过。不要为了“重试一下”直接重复提交同名或同资源任务，先看 events / logs / metrics 判断失败原因。

## 6. GPU Job 异常状态对照

| 现象 | 优先怀疑 |
| --- | --- |
| `PENDING` 过久 | 优先级不足或配额实时不足，用 `job events` 确认 |
| `CREATING` 卡死 | 镜像拉取失败（`ImagePullBackOff`）或节点初始化 |
| `instances` 中部分 Pod `Pending` | 分布式节点调度不均 |
| `events` 出现 `ImagePullBackOff` | `--image` 拼写错误或 registry 不可达 |
| `logs` 为空但 `status=RUNNING` | 主进程未重定向 stdout |
| `status=FAILED` 但无业务报错 | OOM / GPU 显存溢出 / 节点驱逐 / OOMKilled |
| `quota match failed` / 0 候选 | `--quota gpu,cpu,mem` 在当前 workspace 找不到对应规格。用 `inspire resources specs` 重选；多组撞名时加 `--group <name>` 消歧 |

## 7. HPC 异常状态对照

| 现象 | 优先怀疑 |
| --- | --- |
| `slurmctld BackOff` | 镜像不带 Slurm 运行环境 |
| `steps=-/0` | 正文没用 `srun` 启动程序 |
| `nodes=[]` | 调度未分配；可能是配额 / 优先级问题 |
| `status=SUCCEEDED` 但目录 / `stdout.log` / 报告为空 | CPU 并发或内存贴边；应用层应留 `cpus-per-task - 4` 和约 384 MB 内存余量 |
| `quota match failed` / 0 候选 | `--quota gpu,cpu,mem` 在当前 workspace 找不到对应规格。用 `inspire resources specs --usage hpc` 重选；多组撞名时加 `--group <name>` 消歧 |
| `image not found` | 镜像地址不完整；必须是 `host/namespace/name:tag` 全形式 |

## 8. 模型部署：`serving`

`inspire serving` 面向模型部署服务，普通训练 / 预处理任务不要走它。创建部署需要 `inference_serving.create` 或等价权限；CLI 可创建、观察、查看指标、停止和删除服务。

命令列表、参数和单命令功能以 CLI help 为准。先用 `inspire serving --help` 看可用子命令；创建前用 `inspire serving configs --workspace <WORKSPACE>` 看部署约束，用 `inspire resources specs --usage serving --workspace <WORKSPACE>` 找可用 `--quota gpu,cpu,mem`。

创建合同来自网页端 `/jobs/modelDeployment` 的“自定义部署”表单：镜像按可见名称或 `name:tag` 解析成平台 `mirror_id`，资源规格按 serving 专用 `SCHEDULE_CONFIG_TYPE_SERVE` 查询并提交为 `resource_spec_price`。不要用旧 OpenAPI 的 `image_type`、Docker URL 和 `spec_id` 形态来判断 CLI 行为。

创建部署示例：

```bash
inspire serving create --name <name> --model <model-name> --model-version 1 \
  --workspace <WORKSPACE> --project <PROJECT> --group <COMPUTE_GROUP> \
  --quota 1,18,200 --image sandbox-base:ubuntu24.04-py3.12-1.0.0 \
  --command "python serve.py" --port 8000 --priority 1 --dry-run
```

确认 payload 后去掉 `--dry-run` 提交。服务启动后再用 `serving list`、`serving status`、`serving metrics`、`serving stop` 和 `serving delete` 做观察和止损。

优先级与网页一致：`1` 到 `3` 是低优先级，`4` 是普通优先级，`5` 到 `10` 是高优先级。低优先级部署在资源紧张时可能被高优任务回收。

服务已启动但吞吐、显存或副本负载不明时，用 `inspire serving metrics <name>` 看每个 replica 的资源曲线：

```bash
inspire serving metrics <name> --window 30m
inspire serving metrics <name> --metric gpu,gpu_mem,cpu,mem,net_read,net_write --sparkline --no-plot
```
