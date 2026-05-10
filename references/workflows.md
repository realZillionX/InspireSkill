# 三阶段项目工作流

项目要从环境准备、数据处理推进到分布式训练时，先查本手册。这里只给跨阶段编排和验收点；具体命令参数以 CLI `--help` 为准，单领域细节分别看 [notebook.md](notebook.md)、[compute-workloads.md](compute-workloads.md)、[image-management.md](image-management.md) 和 [resources-and-paths.md](resources-and-paths.md)。

## 1. 阶段 A：CPU 空间准备基底环境

先选定一个可上网 CPU 空间起基底 notebook，安装项目依赖、Slurm/Ray/训练依赖，并保存成项目通用镜像。这样后续 notebook、job、HPC、Ray 都复用同一基底，减少冷启动和重复安装。资源条件通过显式参数或 workload profile 传入，不依赖隐式默认值。

> 下述示例中的 `<GROUP>`、`<WORKSPACE>`、`<IMAGE_URL>` 仅为占位格式。实际值以 `inspire resources specs` 和 `inspire config context` 的实时输出为准。

仓库远端路径默认从 `me` path alias 开始；多个 repo 并列时用 `me:<repo>`。如果需要更短名字，先用 `inspire notebook set-path ... as repo` 写入仓库级 alias。

基底 notebook 准备看 [notebook.md](notebook.md)，镜像固化和可见性看 [image-management.md](image-management.md)。一次性临时任务可以跳过 `image save`。

```bash
# 创建并配置基底 notebook -> 安装依赖 -> 保存为项目镜像
inspire notebook create --workspace <WORKSPACE> --group <GROUP> -q 0,20,256 \
  --name <name>-base --image <IMAGE_URL> \
  --project <P> --wait

inspire notebook ssh <name>-base --cwd me
inspire notebook exec <name>-base --cwd me:<repo> "python --version && nvidia-smi || true"
inspire notebook install-deps <name>-base --slurm --ray
inspire image save <name>-base -n <img> -v v1 --public --wait
```

## 2. 阶段 B：CPU 空间跑数据处理

固定规模批处理用 HPC；流式、长守护或异构 worker 才考虑 Ray。

| 形态 | HPC | Ray |
| --- | --- | --- |
| 任务边界 | 明确开始和结束 | 长时间流式或服务型 |
| 并发模型 | 固定 `ntasks × instance_count` | `min/max` 弹性伸缩 |
| 数据流 | GPFS 到处理再到 GPFS | worker 间走 Ray 对象存储 |
| 结束条件 | `srun` 退出自动结束 | driver 退出才结束 |

正式放量前先跑接近生产规模的 probe。小规模通过不代表正式规模稳定。

HPC 和 Ray 的资源模型、示例与状态判断看 [compute-workloads.md](compute-workloads.md)。

## 3. 阶段 C：分布式训练空间

训练空间多数节点不可上网。依赖、权重和数据集先在可上网空间下载到共享盘，再进训练空间。

单节点调试：先用 `inspire notebook create` 在训练空间起 notebook；连接、执行和事件观察看 [notebook.md](notebook.md)。

多节点训练命令和异常判断看 [compute-workloads.md](compute-workloads.md)。提交后用 `job logs --follow` 跟日志：

```bash
inspire job create -n <name>-train -q 8,160,1800 --nodes 2 \
  -c 'bash <repo>/train.sh' --workspace <WORKSPACE> --group <GROUP> \
  --image <IMAGE_URL> --priority 5
inspire job logs --follow <name>-train
```

训练失败或长时间排队时，先查：

```bash
inspire job events <name>-train --tail 50
inspire job logs <name>-train --tail 100
inspire job status <name>-train
```

训练已进入 `RUNNING` 后，把 `metrics` 当成和日志同级的健康度观察面：

```bash
inspire job metrics <name>-train --window 30m
inspire job metrics <name>-train --metric gpu,gpu_mem,cpu,mem,net_read,net_write --sparkline --no-plot
```

多节点训练里，某个 pod 长期低 GPU / 低网络通常意味着数据加载、通信或进程状态异常；所有 pod GPU 接近零且 CPU / I/O 也安静时，不要只盯 `RUNNING`，回到日志和产出文件确认训练是否真的推进。
