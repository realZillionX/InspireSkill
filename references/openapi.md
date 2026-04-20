# 启智 OpenAPI 参考（合并版）

> 本文把官方发布的三份 OpenAPI 文档——**分布式训练**、**模型部署**、**高性能计算**——合并为单一参考，去除重复的认证/错误码/Token 管理章节。InspireSkill 的 cli 层 (`./cli/`) 已对下列接口做了封装，本文供需要直接打 OpenAPI 或调试异常时查阅。
>
> **状态**：启智 OpenAPI 处于 **Alpha** 阶段，后续更新可能无法保证前向兼容；生产级应用请预先联系启智团队。

## 目录

- [1. 通用约定](#1-通用约定)
  - [1.1 基本概念](#11-基本概念)
  - [1.2 服务地址与鉴权](#12-服务地址与鉴权)
  - [1.3 错误码](#13-错误码)
  - [1.4 Token 管理](#14-token-管理)
- [2. 分布式训练（train_job）](#2-分布式训练train_job)
- [3. 模型部署（inference_servings）](#3-模型部署inference_servings)
- [4. 高性能计算（hpc_jobs）](#4-高性能计算hpc_jobs)

---

## 1. 通用约定

### 1.1 基本概念

| 名称 | 说明 |
| --- | --- |
| 账号 | 启智平台的身份凭证，资源归属与计量的主体。使用前需注册生成，用于启智控制台登录和 OpenAPI 调用。 |
| Token | 数字身份标识，携带用户信息。调用 OpenAPI 前需通过账号（用户名/密码）交换 Bearer Token，并在请求头中携带。 |

请求头统一示例：

```http
Authorization: Bearer <token>
```

### 1.2 服务地址与鉴权

- **Host**：`qz.sii.edu.cn`
- **流程**：先 `POST /auth/token` 拿 `access_token`，再在业务接口 header 里附带。

示例：

```bash
curl --location 'https://qz.sii.edu.cn/openapi/v1/train_job/create' \
  --header 'Authorization: Bearer <token>'
```

### 1.3 错误码

| Code | HTTP | Message | 处理措施 |
| --- | --- | --- | --- |
| 429 | 429 | Too Many Requests | 接口触发频控，降低调用频次。 |
| -100000 | 400 | 参数校验错误 | 检查请求参数是否满足业务接口要求。 |
| 500 | 500 | 内部服务错误 | 联系启智 Oncall。 |

### 1.4 Token 管理

#### 获取 Access Token

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/auth/token`

| 参数 | 类型 | 必选 | 说明 |
| --- | --- | --- | --- |
| username | String | 是 | 用户名 |
| password | String | 是 | 密码 |

```bash
curl --location --request POST 'https://qz.sii.edu.cn/auth/token' \
  --data-raw '{"username":"xxx","password":"xxx"}'
```

返回的 `data` 字段：

```json
{
  "access_token": "eyJhxxx",
  "expires_in": "604800",
  "token_type": "Bearer"
}
```

所有业务接口返回结构一致：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| code | Integer | `0` 表示成功，非零表示错误。 |
| message | String | 错误信息。 |
| data | Object | 业务数据。 |

后续章节的「返回参数」均省略 `code`/`message` 字段描述。

---

## 2. 分布式训练（train_job）

| API | 功能 |
| --- | --- |
| Create | 创建分布式训练任务。 |
| Detail | 获取分布式训练任务详情。 |
| Stop   | 停止分布式训练任务。 |

### 2.1 创建训练任务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/train_job/create`

| 参数 | 类型 | 必选 | 示例 | 说明 |
| --- | --- | --- | --- | --- |
| name | String | 是 | `test_api` | 训练任务名称。 |
| logic_compute_group_id | String | 是 | `lcg-xxxx` | 计算资源组 ID。 |
| project_id | String | 是 | `project-xxxx` | 项目 ID。 |
| auto_fault_tolerance | Boolean | 否 | `false` | 是否开启训练容错。 |
| fault_tolerance_max_retry | Integer | 否 | `10` | 开启容错后必填的最大重试次数。 |
| framework | String | 是 | `pytorch` | 训练框架。 |
| command | String | 是 | `sleep 999999` | 启动命令。 |
| task_priority | Integer | 是 | `4` | 任务优先级。 |
| workspace_id | String | 是 | `ws-xxxx` | 工作空间 ID。 |
| framework_config | Array&lt;Object&gt; | 是 | 见下表 | 资源设置信息。 |

`framework_config[]` 子字段：

| 参数 | 类型 | 必选 | 示例 | 说明 |
| --- | --- | --- | --- | --- |
| image | String | 是 | `docker.sii.shaipower.online/inspire-studio/pytorch:25.06-py3` | 镜像名称。 |
| image_type | String | 是 | `SOURCE_OFFICIAL` | 可选 `SOURCE_PUBLIC`、`SOURCE_PRIVATE`、`SOURCE_OFFICIAL`。 |
| instance_count | Integer | 是 | `1` | 实例数。 |
| shm_gi | Integer | 是 | `0` | 共享内存大小。 |
| spec_id | String | 是 | `xxxx` | 规格 ID；可在平台创建 demo 任务后从 `detail` 返回的 `quota_id` 拿到。 |

```bash
curl --location --request POST 'https://qz.sii.edu.cn/openapi/v1/train_job/create' \
  --header 'Authorization: Bearer <token>' \
  --data-raw '{
    "name": "test_api",
    "logic_compute_group_id": "lcg-xxxx",
    "project_id": "project-xxxx",
    "framework": "pytorch",
    "command": "sleep 999999",
    "task_priority": 4,
    "workspace_id": "ws-xxxx",
    "framework_config": [{
      "image": "docker.sii.shaipower.online/inspire-studio/pytorch:25.06-py3",
      "image_type": "SOURCE_OFFICIAL",
      "instance_count": 1,
      "shm_gi": 0,
      "spec_id": "xxxx"
    }]
  }'
```

### 2.2 查询训练任务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/train_job/detail`

| 参数 | 类型 | 必选 | 示例 | 说明 |
| --- | --- | --- | --- | --- |
| job_id | String | 是 | `job-bf6c6a50-841b-4da5-aaa4-2f50af03xxxx` | 任务 ID。 |

### 2.3 停止训练任务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/train_job/stop`

请求参数与 2.2 相同（`job_id`）。

---

## 3. 模型部署（inference_servings）

| API | 功能 |
| --- | --- |
| Create | 创建模型部署服务。 |
| Detail | 获取模型部署服务详情。 |
| Stop   | 停止模型部署服务。 |

### 3.1 创建部署服务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/inference_servings/create`

| 参数 | 类型 | 必选 | 示例 | 说明 |
| --- | --- | --- | --- | --- |
| name | String | 是 | `test-sv` | 服务名称。 |
| logic_compute_group_id | String | 是 | `lcg-xxxx` | 计算资源组 ID。 |
| project_id | String | 是 | `project-xxxx` | 项目 ID。 |
| image | String | 是 | `docker.sii.shaipower.online/inspire-studio/torch_cuda_streaming_video_vllm:xxx` | 镜像名称。 |
| image_type | String | 是 | `SOURCE_PUBLIC` | 可选 `SOURCE_PUBLIC`、`SOURCE_PRIVATE`、`SOURCE_OFFICIAL`。 |
| command | String | 是 | `sleep infinity` | 启动命令。 |
| model_id | String | 是 | `xxxx` | 模型 ID。 |
| model_version | Integer | 是 | `1` | 模型版本。 |
| port | Integer | 是 | `2400` | 服务端口。 |
| replicas | Integer | 是 | `1` | 推理副本数。 |
| node_num_per_replica | Integer | 是 | `1` | 单副本实例数。 |
| custom_domain | String | 否 | `xxxx` | 自定义域名。 |
| task_priority | Integer | 是 | `4` | 任务优先级。 |
| workspace_id | String | 是 | `ws-xxxx` | 工作空间 ID。 |
| spec_id | String | 是 | `xxxx` | 规格 ID；来源同上。 |

```bash
curl --location --request POST 'https://qz.sii.edu.cn/openapi/v1/inference_servings/create' \
  --header 'Authorization: Bearer <token>' \
  --data-raw '{
    "name": "test-sv",
    "logic_compute_group_id": "lcg-xxxx",
    "project_id": "project-xxxx",
    "image": "docker.sii.shaipower.online/inspire-studio/torch_cuda_streaming_video_vllm:xxx",
    "image_type": "SOURCE_PUBLIC",
    "command": "sleep infinity",
    "model_id": "xxxx",
    "model_version": 1,
    "port": 2400,
    "replicas": 1,
    "node_num_per_replica": 1,
    "task_priority": 10,
    "workspace_id": "ws-xxxx",
    "spec_id": "xxxx"
  }'
```

### 3.2 查询部署服务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/inference_servings/detail`

| 参数 | 类型 | 必选 | 示例 | 说明 |
| --- | --- | --- | --- | --- |
| inference_serving_id | String | 是 | `sv-e824d6e8-5a41-4dce-bec2-aafea83bxxxx` | 部署服务 ID。 |

### 3.3 停止部署服务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/inference_servings/stop`

请求参数与 3.2 相同（`inference_serving_id`）。

---

## 4. 高性能计算（hpc_jobs）

| API | 功能 |
| --- | --- |
| Create | 创建高性能计算任务。 |
| Detail | 获取高性能计算任务详情。 |
| Stop   | 停止高性能计算任务。 |

### 4.1 创建 HPC 任务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/hpc_jobs/create`

| 参数 | 类型 | 必选 | 示例 | 说明 |
| --- | --- | --- | --- | --- |
| name | String | 是 | `test_openapi` | 任务名称。 |
| logic_compute_group_id | String | 是 | `lcg-xxxx` | 计算资源组 ID。 |
| project_id | String | 是 | `project-xxxx` | 项目 ID。 |
| image | String | 是 | `docker.sii.shaipower.online/inspire-studio/slurm-gromacs:xxx` | 镜像名称。 |
| image_type | String | 是 | `SOURCE_PUBLIC` | 可选 `SOURCE_PUBLIC`、`SOURCE_PRIVATE`、`SOURCE_OFFICIAL`。 |
| entrypoint | String | 是 | `sleep 1` | 启动命令。 |
| instance_count | Integer | 是 | `1` | 实例数。 |
| task_priority | Integer | 是 | `4` | 任务优先级。 |
| workspace_id | String | 是 | `ws-xxxx` | 工作空间 ID。 |
| spec_id | String | 是 | `xxxx` | 规格 ID；来源同上。 |
| ttl_after_finish_seconds | Integer | 否 | `600` | 结束后保留时长（秒）。 |
| number_of_tasks | Integer | 是 | `2` | 子任务数量。 |
| cpus_per_task | Integer | 是 | `1` | 每个子任务的 CPU 核数。 |
| memory_per_cpu | String | 是 | `4G` | 每个 CPU 的内存。 |
| enable_hyper_threading | Boolean | 是 | `false` | 是否开启超线程。 |

```bash
curl --location --request POST 'https://qz.sii.edu.cn/openapi/v1/hpc_jobs/create' \
  --header 'Authorization: Bearer <token>' \
  --data-raw '{
    "name": "test_openapi",
    "logic_compute_group_id": "lcg-xxxx",
    "project_id": "project-xxxx",
    "image": "docker.sii.shaipower.online/inspire-studio/slurm-gromacs:xxx",
    "image_type": "SOURCE_PUBLIC",
    "entrypoint": "sleep 1",
    "instance_count": 1,
    "spec_id": "xxxx",
    "workspace_id": "ws-xxxx",
    "number_of_tasks": 1,
    "cpus_per_task": 1,
    "memory_per_cpu": "4G",
    "enable_hyper_threading": false
  }'
```

### 4.2 查询 HPC 任务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/hpc_jobs/detail`

| 参数 | 类型 | 必选 | 示例 | 说明 |
| --- | --- | --- | --- | --- |
| job_id | String | 是 | `hpc-job-7768776e-16b5-4b09-a61e-e5341c7dxxxx` | 任务 ID。 |

### 4.3 停止 HPC 任务

- **方法**：`POST`
- **URL**：`https://qz.sii.edu.cn/openapi/v1/hpc_jobs/stop`

请求参数与 4.2 相同（`job_id`）。
