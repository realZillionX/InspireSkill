# 资源、规格与远端路径

选择 `CPU资源空间` / `分布式训练空间`、compute group、`--quota`、存储池、path alias，或解释实例里的路径为什么不可见时，先查本手册。具体 notebook、job、HPC、Ray 和 serving 生命周期看对应业务手册。

## 1. 三类名字

启智任务里容易混淆的名字只有三类：

| 类型 | 作用 | 示例入口 |
| --- | --- | --- |
| 调度名字 | 决定任务在哪跑、用多少资源、用哪个镜像 | `--workspace`、`--project`、`--group`、`--quota`、`--image` |
| 远端路径 alias | 决定代码、数据、权重和产物放在哪 | `me`、`public`、`ssd.me`、`qb-ilm2.public` |
| 对象名字 | 决定观察或清理哪个平台对象 | notebook / job / hpc / ray / serving 的 `<name>` |

调度名字和远端路径 alias 不能混用。`workspace`、`project`、`group`、`quota`、`image` 没有隐式默认值，创建 workload 时显式传入，或用 workload profile 显式填入；path alias 只用于 `--cwd`、`scp`、日志路径和共享盘约定。

## 2. 日常主 Workspace

日常使用不要把 workspace 写得过于抽象。对大多数账号和项目，工作空间选择就是：

| Workspace | 日常用途 |
| --- | --- |
| `CPU资源空间` | CPU notebook、联网准备、依赖安装、HPC 数据处理、CPU Ray |
| `分布式训练空间` | GPU notebook、GPU job、多节点训练、serving、GPU 指标观察 |

国产卡分区、`CI-情境智能` 工作空间或小组专属空间只在任务明确要求特殊硬件、特殊权限或特殊工业模型分区时使用。普通说明和示例应直接写 `CPU资源空间` 或 `分布式训练空间`，不要再把这两个日常空间抽象成占位符。

## 3. 资源查询入口

资源和规格以 CLI help 为准：

```bash
inspire resources --help
inspire job quota --help
inspire notebook quota --help
inspire resources availability --help
inspire resources nodes --help
```

常用判断顺序：

1. `inspire config context` 看可传入命令的 workspace / project / compute group 名字。
2. CPU 准备 / HPC 用 `inspire notebook quota --workspace CPU资源空间` 或 `inspire hpc quota --workspace CPU资源空间` 选合法的 `--quota gpu,cpu,mem` 三元组。
3. GPU 训练 / serving 用 `inspire job quota --workspace 分布式训练空间` 或 `inspire serving quota --workspace 分布式训练空间` 选合法的 `--quota gpu,cpu,mem` 三元组。
4. 在 `<workload> quota`、`resources availability` 和 `resources nodes` 这类查询命令里，`--group` 是 compute group name keyword / substring 过滤，可输入 `H200`、`CPU资源-2` 这类部分关键词，不要求完整名称；`create` / profile 的 `--group` 必须填写完整 compute group 名称。
5. `inspire resources availability --workspace all --include-cpu` 看实时空余。
6. 多节点 GPU 任务再用 `inspire resources nodes --workspace <name|all> --min-nodes <n>` 看整节点空闲。

资源和可用量以平台实时查询为准。本地缓存、历史截图和旧文档不能当作资源事实。

## 4. `--quota` 三元组

`--quota` / `-q` 的格式是：

```bash
<gpu>,<cpu>,<mem>
```

`mem` 以 GiB 计。例如 `1,20,200` 表示 1 张 GPU、20 核 CPU、200 GiB 内存；CPU-only 写 `0,4,32`。

GPU 型号由 workspace 和 compute group 决定，不写进三元组。三元组必须在当前可见规格里唯一匹配；如果多组撞上同一三元组，先用 `<workload> quota --group <keyword>` 查询候选，再在 `create` 或 profile 里填写完整 compute group 名称。

申请资源前先查实时空余，再按真实需求申请。不要因为保守猜测主动缩小规模；只有调度语义或实时空余明确不足时才降档。项目点券通常是项目组级整体限制，个人日常调用算力一般不把它作为首要瓶颈。

## 5. 公网与内部源边界

联网不是全平台默认能力。先区分公网和 SII 内部源：`git clone`、外部数据源和 Hugging Face 权重通常需要公网；PIP、Apt、Conda、npm、Maven、Docker 镜像仓库、OSS 和 NTP 这类平台内部源通常不等同于公网。

需要公网下载时，直接在 `CPU资源空间` 选可上网 CPU compute group 起 notebook。具体组名和 `--quota` 以 `notebook quota` 实时结果为准，日常 notebook 准备盒通常从 `CPU资源-2` 这类 CPU 组开始：

```bash
inspire notebook quota --workspace CPU资源空间 --include-empty
inspire notebook quota --workspace CPU资源空间 --group CPU资源-2
inspire notebook create --workspace CPU资源空间 --group CPU资源-2 -q 0,20,256 \
  --project <PROJECT> --image <BASE_IMAGE> --name prep-box --wait
inspire notebook connection refresh prep-box --workspace CPU资源空间
inspire notebook exec prep-box --cwd me:<repo> "git pull && pip install -r requirements.txt"
```

准备结果有两条去向：

| 去向 | 适用场景 |
| --- | --- |
| 写入共享盘 | 数据集、权重、checkpoint、预处理产物、repo 工作区 |
| 保存为镜像 | Python / system 依赖、Slurm / Ray runtime、服务启动环境 |

`分布式训练空间` 不可上网时，不要在目标 GPU notebook / job 里反复尝试公网下载；先回到 `CPU资源空间` 的可上网 CPU notebook 准备共享盘内容或镜像，再提交目标任务。只缺内部源覆盖的依赖或镜像时，可以直接在目标 GPU notebook 里配置内部源并验证，不必先绕回可上网区。

SII 内部源和公网不是一回事。即使 compute group 没有公网，内部源通常仍可访问；安装包、补系统依赖、拉内部镜像或访问内部对象存储前，先按下面的入口判断内部源是否可用。下表只记录 Agent 需要执行的内部入口；上游镜像和完整背景不要写进日常任务说明。

| 类型 | 地址 / 用法 |
| --- | --- |
| PIP / PyPI | `http://nexus.sii.shaipower.online/repository/pypi/simple/`；`pip download` 场景可用 `http://nexus.sii.shaipower.online/repository/pypi_proxy/simple/` |
| PyTorch wheels | `http://nexus.sii.shaipower.online/repository/pytorch/whl/cu126` |
| Conda | `http://nexus.sii.shaipower.online/repository/anaconda/pkgs/main`；`conda-forge`、`bioconda`、`menpo`、`pytorch` 等 channel 走 `http://nexus.sii.shaipower.online/repository/anaconda/cloud` |
| Ubuntu Apt | `http://nexus.sii.shaipower.online/repository/ubuntu/`；按镜像 codename 选择 `plucky`、`jammy`、`focal` 或 `xenial` |
| Debian 12 Apt | `http://nexus.sii.shaipower.online/repository/debian/`、`http://nexus.sii.shaipower.online/repository/debian-security` |
| ROS / OpenEuler / NVIDIA CUDA | `http://nexus.sii.shaipower.online/repository/ros/`、`http://nexus.sii.shaipower.online/repository/openeuler/`、`http://nexus.sii.shaipower.online/repository/nvidia-cuda/` |
| npm / Node.js | `http://nexus.sii.shaipower.online/repository/npm_proxy/`、`http://nexus.sii.shaipower.online/repository/nodejs/` |
| Maven | `http://nexus.sii.shaipower.online/repository/maven-proxy/` |
| Rust / Cargo | `http://nexus.sii.shaipower.online/repository/rustup/rustup`、`http://nexus.sii.shaipower.online/repository/rustup` |
| Ruby | `http://nexus.sii.shaipower.online/repository/ruby/` |
| Docker 镜像仓库 | `docker-qb.sii.edu.cn`、`docker-qbsandbox.sii.edu.cn`、`docker-t.sii.edu.cn` |
| OSS | `oss-nat.sii.edu.cn:8009` |
| NTP | `ntp0.sii.shaipower.online`、`ntp1.sii.shaipower.online` |

常用快速配置：

```bash
pip3 config set global.index-url http://nexus.sii.shaipower.online/repository/pypi/simple/
pip3 config set global.trusted-host nexus.sii.shaipower.online
npm config set registry http://nexus.sii.shaipower.online/repository/npm_proxy/
```

PyTorch 单独装 CUDA wheel 时，不要走外部 `download.pytorch.org`：

```bash
pip install torch torchvision torchaudio \
  --index-url http://nexus.sii.shaipower.online/repository/pytorch/whl/cu126 \
  --trusted-host nexus.sii.shaipower.online
```

Conda 需要写完整 channel 映射，避免只替换一个 URL 后仍回源到外网：

```bash
cat > ~/.condarc <<'EOF'
offline: false
ssl_verify: false
show_channel_urls: yes
channels:
  - conda-forge
  - bioconda
  - menpo
  - pytorch
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/main
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/free
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/r
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/msys2
default_channels:
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/main
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/r
  - http://nexus.sii.shaipower.online/repository/anaconda/pkgs/msys2
custom_channels:
  conda-forge: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  msys2: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  bioconda: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  menpo: http://nexus.sii.shaipower.online/repository/anaconda/cloud
  pytorch: http://nexus.sii.shaipower.online/repository/anaconda/cloud
EOF
conda clean -i
```

Apt 的黑盒用法是改 `/etc/apt/sources.list`、`sudo apt-get update`、再 `sudo apt-get install <pkg>`。如果镜像是 `jammy`、`focal`、`plucky`、`xenial`、`bookworm` 或其它发行版，不要机械粘贴其它发行版的源行，先确认镜像 codename 和内部源是否有对应路径。

在 notebook 里用内部源跑通依赖后，仍要用 `inspire image save` 保存成镜像，避免后续 workload 每次启动都重新安装。`image save` 会触发一段中等时长的镜像保存过程；保存过程中不可操作该 notebook；保存完毕后 notebook 不会被自动停止，仍可继续连接和使用。

## 6. 远端路径作用域

先决定作用域，再选存储池。账号级 `[path_aliases]` 表达默认远端路径，仓库级、账号隔离的 `[path_aliases]` 表达当前 repo 的覆盖；不要维护单独的“远端工作目录”字段。

| 作用域 | 路径样例 | 定位 |
| --- | --- | --- |
| 项目个人 | `/inspire/<tier>/project/<topic>/<path-user>/...` | 每项目、每账号一份。适合代码、脚本、配置、调试输出 |
| 项目公共 | `/inspire/<tier>/project/<topic>/public/...` | 项目成员共享。适合数据集、权重、批量结果、checkpoint |
| 全局个人 | `/inspire/<tier>/global_user/<path-user>/...` | 跨项目个人盘。适合脚本、配置、小工具和跨项目小文件中转 |
| 全局公共 | `/inspire/hdd/global_public/...` | 全平台共享，普通账号只读，稳定共享物由维护者统一放置 |

`<path-user>` 是平台共享盘返回的个人目录名，不一定等于登录 ID。例如登录账号可以是数字工号，个人目录名可以是拼音加内部编号。

## 7. 存储池

| 池 | 项目路径前缀 | 定位 |
| --- | --- | --- |
| SSD `gpfs_flash` | `/inspire/ssd/project/<topic>/` | 训练 hot path、活跃工作集、checkpoint 热点 |
| HDD `gpfs_hdd` | `/inspire/hdd/project/<topic>/` | 通用空间，写前看剩余容量 |
| qb-ilm `qb_prod_ipfs01` | `/inspire/qb-ilm/project/<topic>/` | 大容量，顺序读带宽接近 SSD |
| qb-ilm2 `qb_prod_ipfs02` | `/inspire/qb-ilm2/project/<topic>/` | 新且空余多，新增大数据默认优先考虑 |

`global_public` 只在 hdd。需要 SSD 或 qb-ilm 速度时，优先走项目个人或项目公共路径。

## 8. 挂载隔离

实例只挂自身所在项目的 fileset。其它项目的 `/inspire/{hdd,ssd,qb-ilm,qb-ilm2}/project/<others>/` 在该实例里通常不存在，`ls` 报 `No such file` 不是权限问题。

跨项目搬小文件时，在两个项目各起一个 notebook，用 `/inspire/hdd/global_user/<path-user>/` 中转。大数据集或全量 checkpoint 超出个人 quota 时，联系项目管理员处理。

## 9. Path Alias 配置入口

默认远端路径由 `inspire init` 写入账号配置 `~/.inspire/accounts/<account>/config.toml`，落在 `[path_aliases]`。需要当前仓库覆盖默认值时，运行 `inspire init --scope project` 写入 `.inspire/accounts/<account>/config.toml`；项目级 alias 会覆盖账号级默认值。查看生效配置用：

```bash
inspire config show --compact
inspire config context
inspire notebook path list
```

默认 alias 语义：

| Alias | 指向 |
| --- | --- |
| `me` | 当前项目、当前账号、初始化时选择的默认存储池 |
| `public` | 当前项目公共目录、初始化时选择的默认存储池 |
| `global-me` | 当前账号全局目录 |
| `<tier>.me` | 指定存储池下的项目个人目录，例如 `ssd.me`、`hdd.me`、`qb-ilm2.me` |
| `<tier>.public` | 指定存储池下的项目公共目录 |
| `<tier>.global-me` | 指定存储池下的全局个人目录 |

路径参数支持三种写法：

```bash
inspire notebook exec <name> --cwd me "pwd"
inspire notebook exec <name> --cwd me:<repo> "git pull"
inspire notebook scp <name> ./config.yaml me:<repo>/config.yaml
```

需要给常用子目录命名时，通过项目级 path alias 命令记录，作为当前 repo 的覆盖或补充：

```bash
inspire notebook path set repo /inspire/ssd/project/<topic>/<path-user>/<repo>
inspire notebook path list
inspire notebook exec <name> --cwd repo "pytest -q"
```

不要把配置文件内容复制到项目说明里；仓库级语义说明写在 `INSPIRE.md`。
