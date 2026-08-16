# 项目工作流

一个项目跨越 CPU 准备、数据处理、GPU 训练、部署或交付时看本页。这里给阶段化决策和验收点，不维护命令模板；具体命令表面回到 CLI Help，单领域边界分别看 Project Context、Resources、Internal Sources、Paths、Dataset、Notebook、Image、Compute Workloads 和 Model References。

## 1. 总体框架

项目上下文是所有阶段的前置：进入项目工作区先按 [`project-context.md`](project-context.md) 核对 `INSPIRE.md` 和项目初始化状态，问清 Project / Workspace / Paths / Image 再动手。之后把项目推进拆成三段：

| 阶段 | 目标 | 典型平台位置 | 验收 |
| --- | --- | --- | --- |
| A. 联网准备 | 下载代码、依赖、数据、权重，形成共享盘布局或镜像 | `CPU资源空间` 的可上网 CPU Notebook | Repo 可更新，依赖可导入，数据 / 权重路径可读，镜像 `READY` |
| B. CPU 处理 | 预处理、清洗、评测、打包、索引构建 | HPC，必要时 Ray | 小规模 Probe 通过，正式规模产物完整，有 Fingerprint |
| C. GPU 训练 / 部署 | 单节点调试、多节点训练、Serving | `分布式训练空间` 的 GPU Notebook、Job、Serving | 日志推进，Metrics 有负载，产物 / 服务 Smoke 通过 |

核心原则：公网和依赖准备前置；目标 GPU 空间只负责读共享盘、拉已准备镜像、运行目标程序。用户指认的专属 Workspace 只有在硬件、权限或项目环境明确要求时才进入计划。

## 2. 阶段 A：准备

准备阶段要产出两类稳定结果：

- 共享盘布局：代码、数据、权重、Checkpoint、输出目录、Fingerprint 约定。
- 可复用镜像：项目依赖、系统依赖、Slurm / Ray Runtime、服务 Runtime。

验收不要只看命令返回成功。至少确认：

- 远端 Repo 能更新到目标 Commit。
- 关键 Python / System 依赖能在目标镜像里导入或执行。
- 数据和权重路径在目标项目共享盘下可读。
- 后续 Workload 要复用的镜像状态为 `READY`。
- Path 约定、基底镜像身份和相关 Workload Profile 名称已按 [`project-context.md`](project-context.md) 写入 `INSPIRE.md`。

## 3. 阶段 B：CPU 处理

固定规模 CPU 批处理优先 HPC；流式、长守护或异构 Worker 才考虑 Ray。选型边界见 [`compute-workloads.md`](compute-workloads.md)。

正式放量前先跑接近生产形状的 Probe。小规模通过不代表正式规模稳定；正式处理要写 Fingerprint，并用同项目 Notebook 回读产物目录确认数量、大小和内容摘要。

## 4. 阶段 C：GPU 训练

进入训练前应已经具备：

- 代码在共享盘 Repo 中，或镜像内已包含固定代码。
- 数据和权重在目标项目共享路径可见。
- 依赖在镜像中，或目标环境无需公网安装。
- 目标 GPU Quota 和 Compute Group 实时可用。
- 单节点 Probe 已验证 CUDA、NCCL、数据路径和入口脚本。

正式多节点训练看四条线：Events 判断调度，Logs 判断程序，Metrics 判断资源是否真的工作，TensorBoard 判断模型本身训得怎么样。前三条都在回答「平台侧这个任务还好吗」，回答不了「loss 还在不在降」——那要对着训练写出的 summary 目录建一个 board，用 `inspire tensorboard scalars` 把曲线当数字读回来，不需要开浏览器。多节点中某个 Pod 长期低 GPU / 低网络时，不要只盯 `RUNNING`，回到该 Worker 日志和数据加载路径。

## 5. 部署或交付

模型服务先把模型目录注册到 Model Registry，再创建 Serving。Serving 验收不止看状态：

- 服务实例齐全，没有长期 Pending。
- Metrics 显示请求或模型加载后的资源曲线合理。
- `api-metrics` 显示请求真的到达且在成功——资源曲线区分不了「没人调用」和「一直调用一直失败」。
- `/health`、`/v1/models` 或业务 Smoke Test 通过。
- 无 Key 请求被拒绝，带 Key 请求成功。
- 模型版本、镜像版本、启动命令和端口写入 `INSPIRE.md` 的复现合同。

非服务型交付则回到共享盘产物：目录结构、Fingerprint、大小、样例文件和下游读取 Smoke 都要确认。
