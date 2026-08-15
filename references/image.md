# Image 管理

选择已有镜像、注册外部镜像、调整可见性或清理镜像时看本页。**把跑通的 Notebook 固化成镜像不在本页**：那是 Notebook 的生命周期动作（`notebook save-image` / `cancel-save-image`），看 [`notebook.md`](notebook.md)；本页只讲保存出来的镜像之后怎么用、怎么共享、怎么清理。Notebook 内准备依赖同样看 [`notebook.md`](notebook.md)；联网准备和内部源看 [`internal-sources.md`](internal-sources.md)。命令语法和参数以 CLI Help 为准。

## 1. 镜像的职责

镜像保存“已经装好的运行环境”，用于 Notebook、Job、HPC、Ray 和 Serving 之间复用。数据集、权重、Checkpoint 和批量产物不进镜像，应放共享盘路径并用 Path Alias 管理。

镜像按 Workspace 的 Registry 存放，不是账号级的单一目录：`notebook save-image --workspace X` 存出的镜像只出现在 X 的 Registry 里。所以 `image list` / `detail` / `register` / `set-visibility` / `delete` 都要求 `--workspace`，它指的是这份 Registry 所属的 Workspace；换一个 Workspace 就是换一份镜像目录，同名镜像在另一个 Workspace 里可能根本不存在。多个 Workspace 可能共用同一份 Registry，需要用哪个 Workspace 名字取镜像时以 `image list` 的实际结果为准。

一个稳定镜像至少满足：

- 状态为 `READY`。
- 目标 Workspace / Project 有权限读取。
- 训练、HPC、Ray 或 Serving 所需 Runtime 已在同类环境中验证。
- 没有账号 Token、私有 Wheel、内部数据或临时调试文件。

## 2. 选择路径

| 目标 | 路径 | 判断 |
| --- | --- | --- |
| 使用官方或已有自定义镜像 | `image list` / `image detail` | 镜像存在、状态可调度、权限可见 |
| 固化运行中的 Notebook | `notebook save-image`（见 [`notebook.md`](notebook.md)） | 依赖是在平台 Notebook 里装好的 |
| 纳入外部 Docker 镜像 | `image register` | 镜像在本地、CI 或外部 Registry 构建完成 |
| 调整共享范围 | `image set-visibility` | 协作者需要复用，或实验镜像应收回私有 |
| 删除镜像 | `image delete` | 确认没有活跃 Workload 或协作者依赖 |

镜像刚保存或刚注册时，不要只看创建命令成功；必须等到 `READY` 后再用于调度。

## 3. 可见性边界

默认可见性按风险选：敏感依赖、个人实验和含内部调试文件的镜像保持 private；团队要复用且确认无 secret 后再 public。`notebook save-image --visibility` 在保存时表达同一个选择，之后仍可用 `image set-visibility` 改。

保存出的镜像成为项目基底或被后续 Workload 长期复用时，把名称、用途和覆盖的依赖回填到 `INSPIRE.md`（见 [`project-context.md`](project-context.md)）。

## 4. Register 边界

`image register` 适合外部镜像，不适合保存运行中的 Notebook——后者走 `notebook save-image`。Push 工作流是平台给出 Registry 槽位，Agent 推镜像；Address 工作流是登记已有 Registry 地址。

注册后一直无法 `READY` 时，优先怀疑 Registry 权限、镜像地址不完整、Tag 不存在或目标 Workspace 无法访问该 Registry。

## 5. 清理原则

只删除确认不再使用的自定义镜像。清理前至少确认：

- 没有 Running 或 Pending 的 Notebook、Job、HPC、Ray 或 Serving 依赖它。
- Batch 文件、Profile 或协作约定不再引用它。
- 协作者不再用这个版本复现实验。

被取消的保存会在目录里留下一条 `FAILED` 镜像记录，取消命令本身不清理它；确认不再需要后用 `image delete` 单独删掉。
