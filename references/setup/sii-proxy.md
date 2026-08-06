# Clash Verge SII 分流

本页只处理本机 Clash Verge 对 `*.sii.edu.cn` 的分流，以及公网代理与 SII 校园网直连之间的切换。账号级 proxy、Shell proxy、`NO_PROXY` 和有效路由诊断见 [`install-and-config.md`](install-and-config.md)；受限 Notebook 的连接与文件流转见 [`../notebook.md`](../notebook.md)。

- [配置目标](#1-配置目标)
- [脚本模板](#2-脚本模板)
- [验证与诊断](#3-验证与诊断)

## 1. 配置目标

把 `*.sii.edu.cn` 放入独立的 `SII Proxy` 选择组：

- 公网环境选择组织分发的 SII proxy 节点。
- 能直连 SII 校园网时选择 `DIRECT`。
- 其它流量继续沿用订阅原有规则。

Clash Verge Rev 的脚本常见路径：

```text
~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles/Script.js
```

先在 Clash Verge 设置页确认本机 mixed port，再把下面逻辑合并进 `Script.js`。将 `<...>` 替换为组织分发的真实 host、port、user 和 password；真实凭据不得进入公开仓库或聊天记录。节点数量变化时，同步修改 `SII_PROXY_NAMES`、`SII_PROXIES` 和 `SII_MANAGED_PROXY_NAMES`；始终保留 `DIRECT`。

## 2. 脚本模板

```javascript
var SII_PROXY_GROUP_NAME = "SII Proxy";
var SII_PROXY_NAMES = ["SII Proxy 1", "SII Proxy 2", "DIRECT"];
var SII_PROXIES = [
  {
    name: "SII Proxy 1",
    type: "socks5",
    server: "<sii-proxy-host-1>",
    port: <sii-proxy-port-1>,
    username: "<sii-proxy-user-1>",
    password: "<sii-proxy-password-1>",
    tls: false,
    udp: true,
    "skip-cert-verify": true
  },
  {
    name: "SII Proxy 2",
    type: "socks5",
    server: "<sii-proxy-host-2>",
    port: <sii-proxy-port-2>,
    username: "<sii-proxy-user-2>",
    password: "<sii-proxy-password-2>",
    tls: false,
    udp: true,
    "skip-cert-verify": true
  }
];

var SII_PROXY_GROUP = {
  name: SII_PROXY_GROUP_NAME,
  type: "select",
  proxies: SII_PROXY_NAMES
};

var SII_MANAGED_PROXY_NAMES = {
  "SII Proxy 1": 1,
  "SII Proxy 2": 1
};

function ensureArray(value) {
  return Array.isArray(value) ? value : [];
}

function forceUnshift(rules, rule) {
  var index = rules.indexOf(rule);
  if (index !== -1) rules.splice(index, 1);
  rules.unshift(rule);
}

function upsertProxy(proxies, newProxy) {
  var output = [];
  for (var index = 0; index < proxies.length; index++) {
    if (proxies[index] && proxies[index].name === newProxy.name) continue;
    output.push(proxies[index]);
  }
  output.push(newProxy);
  return output;
}

function removeProxyNames(proxies, names) {
  var output = [];
  for (var index = 0; index < proxies.length; index++) {
    var proxy = proxies[index];
    if (proxy && proxy.name && names[proxy.name]) continue;
    output.push(proxy);
  }
  return output;
}

function resetSiiProxy(config) {
  config.proxies = removeProxyNames(ensureArray(config.proxies), SII_MANAGED_PROXY_NAMES);
  config["proxy-groups"] = ensureArray(config["proxy-groups"]).filter(function(group) {
    return group && group.name !== SII_PROXY_GROUP_NAME;
  });
}

function injectSiiProxy(config) {
  config.proxies = ensureArray(config.proxies);
  for (var index = 0; index < SII_PROXIES.length; index++) {
    config.proxies = upsertProxy(config.proxies, SII_PROXIES[index]);
  }

  config["proxy-groups"] = ensureArray(config["proxy-groups"]);
  config["proxy-groups"].unshift(SII_PROXY_GROUP);

  var rules = ensureArray(config.rules);
  forceUnshift(rules, "DOMAIN-SUFFIX,sii.edu.cn," + SII_PROXY_GROUP_NAME);
  config.rules = rules;
}

function main(config, profileName) {
  resetSiiProxy(config);
  injectSiiProxy(config);
  return config;
}
```

## 3. 验证与诊断

将 `<mixed-port>` 替换为 Clash Verge 当前 Mixed Port，依次验证本地监听、SII 路由和 Inspire 配置：

```bash
lsof -iTCP:<mixed-port> -sTCP:LISTEN
curl -sS -o /dev/null -w "sii: %{http_code}\n" -x http://127.0.0.1:<mixed-port> https://qz.sii.edu.cn
inspire config check
```

`qz.sii.edu.cn` 访问失败时，依次确认：

1. Clash Verge 规则包含 `DOMAIN-SUFFIX,sii.edu.cn,SII Proxy`。
2. `SII Proxy` 组选择了可用代理节点或当前网络可用的 `DIRECT`。
3. `inspire config show --compact --filter Proxy` 显示的账号级 proxy、Shell proxy、有效路由和 `NO_PROXY` 匹配符合预期。
