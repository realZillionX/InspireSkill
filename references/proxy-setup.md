# Clash Verge 7897 分流配置

> 适用人群：不常驻 SII 的科研人员，与启智平台内网直连受限。
>
> 使用 Clash Verge 将**公网流量**和 **`*.sii.edu.cn` 启智流量**复用同一个本机 `7897` mixed port，通过域名规则分流。这样做的好处：
>
> 1. **抛弃 aTrust**，不再因多人共用账号导致频繁断连。
> 2. **公网与启智代理共存**，Agent（Claude Code / Codex / Gemini CLI）在一条 CLI 执行全流程实验时不会被 VPN 占用副作用干扰。

## 凭据获取

以下片段内 `<sii-proxy-host>`、`<sii-proxy-user>`、`<sii-proxy-password>` 均为占位符，**须替换为你所在实验室 / 组织管理员分发的真实值**。不要把真实凭据提交到任何公开仓库或聊天记录中。

## 启用步骤

1. 打开 **Clash Verge**。
2. 进入订阅对应的 **全局扩展脚本编辑界面**。
3. 将下面的脚本**完整**填入并保存。
4. **重新应用订阅** 或 **重载配置**。
5. 确认 Clash Verge 最终只监听本地 `mixed-port: 7897`，且脚本自动把 `Sii-Proxy` 节点和 `DOMAIN-SUFFIX,sii.edu.cn,Sii-Proxy` 规则插入生效配置中。

## 扩展脚本

```javascript
// Define main function (script entry)

function ensureArray(value) {
  return Array.isArray(value) ? value : [];
}

function prependUniqueRules(existingRules, rulesToPrepend) {
  var filtered = [];
  for (var i = 0; i < existingRules.length; i += 1) {
    if (rulesToPrepend.indexOf(existingRules[i]) === -1) {
      filtered.push(existingRules[i]);
    }
  }
  return rulesToPrepend.concat(filtered);
}

function upsertNamedProxy(existingProxies, proxyToInsert) {
  var filtered = [];
  for (var i = 0; i < existingProxies.length; i += 1) {
    var proxy = existingProxies[i];
    if (!proxy || typeof proxy !== "object") continue;
    if (proxy.name === proxyToInsert.name) continue;
    filtered.push(proxy);
  }
  return [proxyToInsert].concat(filtered);
}

function patchSiiProxy(config) {
  var siiProxy = {
    name: "Sii-Proxy",
    type: "socks5",
    server: "<sii-proxy-host>",
    port: 10808,
    username: "<sii-proxy-user>",
    password: "<sii-proxy-password>",
    tls: false,
    udp: true,
    "skip-cert-verify": true
  };

  var siiRules = [
    "DOMAIN-SUFFIX,sii.edu.cn,Sii-Proxy"
  ];

  config.proxies = upsertNamedProxy(ensureArray(config.proxies), siiProxy);
  config.rules = prependUniqueRules(ensureArray(config.rules), siiRules);

  // Use only the Clash Verge mixed port; domain rules decide the upstream route.
  delete config.port;
  delete config["socks-port"];
  config["mixed-port"] = 7897;
}

function main(config) {
  patchSiiProxy(config);
  return config;
}
```

## 验证

生效后用以下命令自测：

```bash
# 公网（走默认出口，不走 Sii-Proxy）
curl -sS -o /dev/null -w "public: %{http_code}\n" \
  -x socks5h://127.0.0.1:7897 https://www.google.com

# 启智（经 DOMAIN-SUFFIX 规则转给 Sii-Proxy）
curl -sS -o /dev/null -w "sii:    %{http_code}\n" \
  -x socks5h://127.0.0.1:7897 https://qz.sii.edu.cn
```

两条命令都应该返回 `2xx` / `3xx`。如果启智那条返回 `000` 或连接超时，先检查：

1. Clash Verge 是否确实在监听 `127.0.0.1:7897`（`lsof -iTCP:7897 -sTCP:LISTEN`）。
2. 扩展脚本是否已保存并重载（规则面板里能看到 `DOMAIN-SUFFIX,sii.edu.cn,Sii-Proxy`）。
3. 凭据是否正确，且上游 Sii-Proxy 节点仍在服务期内。

## 与 InspireSkill 的衔接

**CLI 不绑定 `7897`**。它只是一个通用的代理消费者，从 `config.toml`（`[proxy]` 下的 `requests_http / requests_https / playwright / rtunnel`）或对应环境变量（`INSPIRE_REQUESTS_HTTP_PROXY` 等）读取代理地址，你给什么地址就走什么地址。本文档把 `7897` 作为示例，是因为 Clash Verge 默认就监听这个端口；如果你用其他代理（原生 SOCKS5 / HTTP 代理、其他端口的 Clash 配置、公司 VPN 等），把对应的 `127.0.0.1:<port>` 填进上面这些字段即可，InspireSkill 不关心端口号。

按上面的模板配好 Clash Verge 之后，把 `127.0.0.1:7897` 写到 `config.toml` 的 `[proxy]` 或导出对应环境变量，CLI 就会自动把公网和 `*.sii.edu.cn` 流量都路由过去。若有异常，可用：

```bash
inspire config show --compact   # 确认 proxy 字段和来源
inspire --debug resources list  # 看调试日志里代理的真实走向
```
