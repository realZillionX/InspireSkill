# 数据广场 API（`aip.sii.edu.cn`）

> **文档类型**：CLI 维护者参考。启智控制台的 Browser API 记在 [`browser-api.md`](browser-api.md) 和 [`browser-api-actions.md`](browser-api-actions.md)。

**数据广场（上海创智学院数据广场）不是启智的一部分**：不同 host、不同 API 风格、不同 Session，只共用同一套 CAS SSO。启智控制台侧边栏的「数据集」是外链过来的，`qz` 那侧只有一个 `dataset.ValidateDataset` 负责把某个版本挂进容器，**检索、目录、版本和权限全在这边**。

两套标识符必须分清：`datasetCode` / `versionCode` 是数据集的用户可见身份，是挂载 API 接受的值、容器路径的组成部分、CLI 唯一展示的东西；`datasetId` / `versionId` 是数据广场内部句柄，`findDatasets` 需要它，拿去挂载会被启智拒为「数据集不存在」——因此它们只活在 resolver 内部，不出现在任何 CLI 表面。

实现在 [`cli/inspire/platform/web/plaza/`](../../cli/inspire/platform/web/plaza/)。

## 1. 握手

纯 HTTP，三步，不需要浏览器——CLI 的 Web Session 里已经有 CAS 的 ticket-granting cookie：

```
1. GET  https://cas.sii.edu.cn/cas/login?service=<urlencode("https://aip.sii.edu.cn/")>
        带现有 Session 的 CASTGC → 302，Location 上带 ?ticket=ST-…      （必须 allow_redirects=False）
2. POST https://aip.sii.edu.cn/api/base/login   {"ticket": "…", "service": "https://aip.sii.edu.cn/"}
        → 下发 `datasets-session` cookie，body 里带 userInfo
3. 之后的调用带该 cookie；前端还会发 `x-user-id: <userInfo.ID>`，实测不带也通
```

- **CAS 的登录路径是 `/cas/login`，不是 `/login`**——后者 404。
- **第 1 步必须 `allow_redirects=False`**，否则 ticket 被 SPA 首页消费掉。
- 拿不到 ticket 说明 **CAS 不认这个 cookie**，也就是平台 Session 过期了，不是数据广场的问题。
- 握手只有两次请求，所以签入后的客户端**只在进程内缓存**，按 `(账号, Session 创建时间)` 作键；刻意不落盘，避免留下过期或跨账号的状态。

## 2. 信封与错误

```jsonc
{"code": 0, "data": {…}, "msg": "…"}
```

**不是 AWS 风格**，`code != 0` 即失败，`msg` 是原因。

判定顺序与启智那侧一致：**限流与服务端故障先于读 body**（限流回的是错误页而不是 JSON 信封），然后 3xx / 401，最后才看 `code`。

- **HTTP 状态码基本无意义，唯一有意义的是 401**：未登录返回 `401 {"code": 7, "msg": "未登录或非法访问"}`，这是重新握手的信号。
- **`findDatasets` 的失败和未登录共用 `code: 7`**：坏 `datasetId` 是 **HTTP 200** + `查询失败:record not found`，未登录是 **HTTP 401**。**判断要不要重新握手只能看 HTTP 状态码，不能看 `code`。**
- 401 时的续期顺序是：重新握手 → 还不行就 `get_web_session(force_refresh=True)` 重建平台 Session 再握手。重新登录很贵，而多数过期只是数据广场自己的。
- 限流走与启智同一套 `TransientAPIError` 退避重试。

## 3. 已封装的端点

| 端点 | 输入 | 输出（`data` 内） | CLI |
| --- | --- | --- | --- |
| `GET /api/datasets/getDatasetsList` | query：`page`、`pageSize`、`keyword?`、`tags?` | `{list[], total, page, pageSize}`（同层） | `dataset list`，以及按 code 定位时的 resolver |
| `POST /api/datasets/findDatasets` | `{datasetId}` | 详情 + `versions[]` | `dataset show` |
| `GET /api/datasetTags/getDatasetTagsList` | query：`pageSize`（前端发 999） | `{list[]}` | `dataset tags`，以及 `--tag` 名字→handle 解析 |

**参数语义与限制**

- **`tags` 是逗号连接的数字 tagId，语义为 OR**；但**空字符串不是通配符**，`tags=""` 返回 0 行。前端在没有选中标签时把这个键整个删掉，客户端也必须省略，不能急着把查询字典拼满。
- **`pageSize` 无上限**，1000 一次取回全部；`page` 越界返回空 `list` 但 `total` 仍然正确。
- **`keyword` 连描述一起匹配**且不区分大小写，命中范围通常比预期宽——**按 code 定位必须在结果里取精确相等的那一行，不能取第一条**。短 code 可能被别的数据集描述里的散文命中挤到后面，此时按 `total` 再取一次全量而不是逐页走。
- **`filesSize` 的单位是 MiB**；**`dataFormats` 是一个 JSON 编码的字符串**，不是数组。
- **`hasPermission` 按账号给**，为 false 的数据集挂载时报 `2005 无访问权限`；**申请权限只有网页端有入口**，CLI 只能把这一列如实报出来。
- **`super` 是数据分级**，取值 `S1` 保密数据 / `S2` 有限访问控制数据 / `S3` 学院内部数据 / `S4` 公开数据。当前账号上 `hasPermission` 完全是它的函数：532 条里 S4 261 条与 S3 165 条全部可挂载，S2 106 条全部不可，S1 一条都看不到。**这个对应关系是账号级的授权结果，不是平台合同**——换一个账号 S3 也可能关着，所以判断能不能挂仍然要读 `hasPermission`，不能拿 `super` 推。
- **`state` 有四个值**（`active` / `wanted` / `processing` / `error`），版本另有 `downloading` / `pending_upload`。
- **`datasetCode` 全表唯一，标签名也唯一**，两个 name→handle 映射都不会歧义。
- **版本号不保证是 `vN`**，实际存在 `v1-br`、`2026-07-30`、`v3again`，不要按序号猜。
- 标签分类 `categoryId`：`0` / `1` = 文本（前端把 0 折进文本，客户端也照做）、`2` = 图像、`3` = 音频、`4` = 视频、`5` = 多模态。

## 4. 存在但未封装

### 权限申请流

`hasPermission` 为 false 的数据集只能申请授权，而**申请入口目前只有网页端**，这是 CLI 侧唯一有明确用户价值的未封装面。端点形状（来自 SPA bundle 与三个 GET 的只读实测，写端点未发起过请求）：

| 端点 | 方法 | 输入 | 说明 |
| --- | --- | --- | --- |
| `datasetApplyApprove/getDatasetApplyList` | GET | `{page, pageSize, keyword}` | 我提交的申请。行含 `id, datasetName, authorityName, applyTime, approveTime, approveUser, applyDescr, state` |
| `datasetApplyApprove/getDatasetApproveList` | GET | `{page, pageSize, keyword, role}` | 待我审批。行另含 `applyUser, projectName` |
| `datasetApplyApprove/intoApproveById` | GET | `{id}` | 单条审批详情，含 `datasetCode` |
| `datasetApplyApprove/datasetApply` | POST | `{applyUserid, applyUserName, applyDescr, datasetId, projectId, applyType}` | 提交申请 |
| `datasetApplyApprove/datasetApprove` | POST | `{id, state}` | **一个端点三用**：`1` 同意、`2` 驳回、`-1` 申请人撤回 |
| `datasetUserRole/createDatasetUserRole` | POST | `{datasetCode, datasetId, projectId, userType, userName, userId, roleId}` | 赋权 |

`state` 语义：`0` 待审批 / `1` 已通过 / `2` 已驳回 / `-1` 已撤回。

接之前有两个坑：`datasetApply` 除了 `datasetId`（resolver 已有）还要 `projectId`，而**广场侧的项目句柄在 CLI 表面完全不存在**，等于要新引入一个项目名→handle 的广场 resolver（来源是 `project/getProjectListByUser`）；另外撤回与审批共用 `datasetApprove`，受控验证要覆盖三种 `state`。

### 其余未封装端点

目录与版本：`getDatasetsWithVersions`（不带参数即返回全目录的 `[{datasetCode, versions:[{versionCode, versionId}]}]` 扁平索引，**不是信封分页，`data` 直接是数组**；一次请求拿到全部 code→版本映射，如果以后要做 `<名字>:<版本>` 的本地预校验，这比现在的 resolve + `findDatasets` 两步便宜）、`checkHuggingFaceDataset`、`datasetLicenses/{list,detail,download,delete}`、`datasetForks/getDatasetForksList`、`sftpgo/user/files`、`project/{getProjectListByUser,findProject}`。

写操作：`createDatasets`、`createDatasetVersion`、`updateDatasets`(PUT)、`updateDatasetsValue`、`deleteDatasets`、`checkDatasetsName`、`datasetVersion/{updateDatasetVersion,deleteDatasetVersion,confirmComplete}`、`datasetUserRole/*`。CLI 不创建也不编辑数据集，这一族没有动线。

### `getDatasetsListUserCenter`：查过，刻意不接

数据管理页「我的数据集」视图，参数是 `{keyword, page, pageSize, projectId, role}`，信封与主目录一致。**不封装的理由不是没查，是查完不该接**：

- 它是 owner / 角色过滤，不是「我项目下的数据集」——某个项目在主目录有 267 行、在这里 0 行。对不持有任何数据集角色的普通账号**恒为空**，做成 `--mine` 等于给出一个永远回「无」的开关。
- 行字段与主目录**存在别名差异**（SPA 自己就在做 `datasetId||id`、`datasetName||name`、`datasetCode||code` 的归一化）。当前账号一行都取不到，投影无法验证，属于不闭合的合同。
- 它是写操作视图的读取面：那一页的存在理由是创建 / 编辑 / 赋权，全部超出 CLI 动线。
- 主目录的 `getDatasetsList` 忽略 `projectId` 和 `role`，所以这两种收窄确实无法用已封装端点复现——但需要它们的场景在 CLI 里也不存在。
