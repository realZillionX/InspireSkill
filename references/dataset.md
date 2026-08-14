# 官方数据集

挂载数据广场的官方数据集，或判断某个数据集能不能用时看本页。共享盘路径和 Path Alias 看 [`paths.md`](paths.md)，Workload 创建条件看 [`resources.md`](resources.md)。命令语法以 CLI Help 为准。

## 1. 数据集不在启智上

数据广场是独立平台，和启智共用同一套 SSO，但目录、权限和版本都由它自己维护。启智只负责把某个版本挂进容器，不提供检索。

因此判断顺序固定：先在数据广场确认数据集和版本存在且当前账号有权限，再回到启智创建 Workload。`inspire dataset` 覆盖前半段，`--dataset` 覆盖后半段。

申请权限只能在数据广场网页端完成，CLI 不提供申请入口。

## 2. 数据集用名字寻址，不用数字 ID

数据广场内部有 `datasetId` / `versionId` 两个数字主键，它们**不能**用于挂载：填数字会被拒为 `数据集不存在`。挂载认的是数据集名和版本名，也就是 `inspire dataset list` 和 `inspire dataset show` 展示的那两列。

| 概念 | 形状 | 举例 |
| --- | --- | --- |
| 数据集名 | 小写短横线字符串 | `pixabay-81k` |
| 版本名 | 自由格式，不保证是 `vN` | `v0`、`v1-br`、`2026-07-30` |
| 挂载参数 | `<数据集名>:<版本名>` | `--dataset pixabay-81k:v0` |

版本名不要按 `v0` / `v1` 递增去猜，一律从 `dataset show` 读。

## 3. 列表里的四个判断字段

| 字段 | 含义 | 影响 |
| --- | --- | --- |
| Access | 当前账号是否有权挂载 | 无权限时创建会被拒为 `无访问权限`，约五分之一的数据集属于这种 |
| State | 数据集状态 | `active` 可用；`processing` / `wanted` / `error` 都不可挂载 |
| Grade | 数据分级 `S2` / `S3` / `S4` | 决定合规要求，不影响命令 |
| Tags | 领域标签 | 五个分类：文本、图像、音频、视频、多模态 |

版本另有自己的状态，`downloading` 和 `pending_upload` 表示数据还没落盘。数据集 `active` 不代表每个版本都可用。

`--keyword` 同时匹配名称和描述，命中范围通常比预期宽；`--tag` 可重复，多个标签之间是 OR。

## 4. 挂载语义

`notebook`、`job` 和 `hpc` 的 `create` 支持 `--dataset`，可重复。`ray` 和 `serving` 不支持——平台直接拒绝该字段，网页端对应表单也没有这一项，需要数据时走共享盘。

挂载点固定为 `/inspire/dataset/<数据集名>/<版本名>`，**只读**。它不受 Path Alias 管辖，也不占项目共享盘配额。

创建前平台会逐条校验并解析真实存储路径，任何一条不通就整体失败，不会先建出一个缺数据的 Workload。想在创建之前单独确认，用 `inspire dataset validate ... --workspace <workspace>`，它按条给出拒绝原因：数据集不存在、版本不存在，或当前账号在该 Workspace 无访问权限。

## 5. 最短闭环

```bash
inspire dataset list --keyword pixabay
inspire dataset show pixabay-81k
inspire dataset validate pixabay-81k:v0 --workspace CPU资源空间
inspire notebook create -n prep --workspace CPU资源空间 --project <project> \
  --group CPU资源-2 -q 0,4,32 --image <image> --dataset pixabay-81k:v0
```

`dataset show` 的每个版本行直接给出可粘贴的 `--dataset` 值和容器内路径，不需要自己拼。
