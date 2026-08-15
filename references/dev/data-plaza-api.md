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
| `GET /api/datasets/getDatasetsList` | query：`page`、`pageSize`、`keyword?`、`tags?` | `{list[], total}`（同层） | `dataset list`，以及按 code 定位时的 resolver |
| `POST /api/datasets/findDatasets` | `{datasetId}` | 详情 + `versions[]` | `dataset show` |
| `GET /api/datasetTags/getDatasetTagsList` | query：`pageSize`（前端发 999） | `{list[]}` | `dataset tags`，以及 `--tag` 名字→handle 解析 |

**参数语义与限制**

- **`tags` 是逗号连接的数字 tagId，语义为 OR**；但**空字符串不是通配符**，`tags=""` 返回 0 行。前端在没有选中标签时把这个键整个删掉，客户端也必须省略，不能急着把查询字典拼满。
- **`pageSize` 无上限**，1000 一次取回全部；`page` 越界返回空 `list` 但 `total` 仍然正确。
- **`keyword` 连描述一起匹配**且不区分大小写，命中范围通常比预期宽——**按 code 定位必须在结果里取精确相等的那一行，不能取第一条**。短 code 可能被别的数据集描述里的散文命中挤到后面，此时按 `total` 再取一次全量而不是逐页走。
- **`filesSize` 的单位是 MiB**；**`dataFormats` 是一个 JSON 编码的字符串**，不是数组。
- **`hasPermission` 按账号给**，为 false 的数据集挂载时报 `2005 无访问权限`；**申请权限只有网页端有入口**，CLI 只能把这一列如实报出来。
- **`state` 有四个值**（`active` / `wanted` / `processing` / `error`），版本另有 `downloading` / `pending_upload`。
- **`datasetCode` 全表唯一，标签名也唯一**，两个 name→handle 映射都不会歧义。
- **版本号不保证是 `vN`**，实际存在 `v1-br`、`2026-07-30`、`v3again`，不要按序号猜。
- 标签分类 `categoryId`：`0` / `1` = 文本（前端把 0 折进文本，客户端也照做）、`2` = 图像、`3` = 音频、`4` = 视频、`5` = 多模态。

## 4. 存在但未封装

`getDatasetsListUserCenter`、`datasetApplyApprove/*`（权限申请流）、`createDatasets`、`createDatasetVersion`、`updateDatasetsValue`、`checkDatasetsName`、`datasetUserRole/*`。当前都没有 CLI 消费者。
