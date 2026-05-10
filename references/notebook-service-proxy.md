# Notebook HTTP 服务暴露

需要把 notebook 容器里已经启动的 HTTP 服务提供给浏览器、OpenAI SDK、Gradio、FastAPI、SGLang、vLLM 或其它客户端时，查本手册。普通 notebook 创建、执行、传文件和基底环境准备看 [notebook.md](notebook.md)。

## 1. 适用边界

Notebook HTTPS proxy 只解决“外部客户端如何到达容器内某个 HTTP 端口”。它不替代服务自身鉴权，也不负责启动业务进程。

常见场景：

| 场景 | 做法 |
| --- | --- |
| 容器里跑 Gradio / FastAPI | 服务监听 `<container-port>`，对外使用 notebook proxy URL |
| 容器里跑 OpenAI-compatible API | base URL 指向 `/proxy/<container-port>/v1` |
| 小组内临时共享 demo | 使用 notebook proxy URL，并在应用层开启登录或 API key |
| 本机 CLI 文件操作 | 不用本文，直接用 `notebook ssh`、`exec`、`shell`、`scp` |

## 2. 发布流程

先确认服务在容器内监听目标端口：

```bash
inspire notebook exec <name> "ss -ltnp | grep ':<container-port>'"
inspire notebook exec <name> "curl -sS http://127.0.0.1:<container-port>/health"
```

建立一次 notebook 连接，让 CLI 写入连接信息：

```bash
inspire notebook ssh <name>
```

读取 notebook proxy URL 模板：

```bash
inspire --json notebook connections --no-check \
  | jq -r '.data.bridges[] | select(.name == "<name>").proxy_url'
```

把模板里的 `/proxy/<cached-port>/` 改成服务端口 `/proxy/<container-port>/`，再追加服务路径。不要手写 URL 里其它路径段；它们由平台和连接缓存生成。例如容器内 OpenAI-compatible 服务监听 `30000` 时，把模板中的 proxy 端口替换为 `30000` 后，在末尾追加 `/v1`。

浏览器类服务直接访问对应路径；SDK 类服务把它填成 base URL。

## 3. 安全边界

- Notebook proxy URL 受启智登录态、项目权限和 URL token 约束；不要把带 token 的 URL 发到公开渠道。
- Notebook proxy 是网络通路，不是业务鉴权。对 LLM API、Gradio、FastAPI 等可消费算力或数据的服务，应在服务本身开启 API key、登录或其它应用层鉴权。
- 不要用本机临时 gateway 直接绑定 `0.0.0.0` 给小组共享；这会绕开启智访问控制，把安全边界变成本机防火墙和局域网状态。
- 服务启用 API key 时，发布前做无 key / 有 key 对照：无 key 请求应返回 `401` 或等价拒绝；带 key 的 `/health`、`/v1/models` 或业务 smoke test 应返回成功。

## 4. 使用示例

```bash
# 容器内确认服务存在
inspire notebook exec <name> "curl -sS http://127.0.0.1:30000/v1/models"

# 本机取得 proxy URL 模板
inspire --json notebook connections --no-check \
  | jq -r '.data.bridges[] | select(.name == "<name>").proxy_url'
```

将输出 URL 中的 `/proxy/<cached-port>/` 替换为 `/proxy/30000/`，OpenAI-compatible base URL 追加到 `/v1`。
