# SII Proxy

本页只处理本机 Clash Verge 的 `*.sii.edu.cn` 分流，以及公网代理与 SII 校园网直连之间的切换。账号级 proxy、Shell proxy、`NO_PROXY` 和有效路由诊断见 [`install-and-config.md`](install-and-config.md)；受限 Notebook 的远端 transport 与合规边界遵循 [`../../SKILL.md`](../../SKILL.md) 和 [`../notebook.md`](../notebook.md)。

Clash Verge 的目标是把 `*.sii.edu.cn` 分到独立的 `SII Proxy` 组。公网环境在这个组里选择 SII proxy 节点；SII 校园网环境选择 `DIRECT`；其它流量沿用订阅规则。

Clash Verge Rev 的脚本常见路径：

```text
~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles/Script.js
```

先在 Clash Verge 设置页确认本机 mixed port，再在 `Script.js` 里合并下面的 SII 分流模板。节点数量、节点端口和 mixed port 按实际环境填写；将 `<...>` 替换为组织分发的 SII proxy host、port、user 和 password，并保持真实凭据不进入公开仓库或聊天记录。保留 `DIRECT` 供校园网直连使用。

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

function ensureArray(v) {
  return Array.isArray(v) ? v : [];
}

function forceUnshift(rules, rule) {
  var idx = rules.indexOf(rule);
  if (idx !== -1) rules.splice(idx, 1);
  rules.unshift(rule);
}

function upsertProxy(proxies, newProxy) {
  var out = [];
  for (var i = 0; i < proxies.length; i++) {
    if (proxies[i] && proxies[i].name === newProxy.name) continue;
    out.push(proxies[i]);
  }
  out.push(newProxy);
  return out;
}

function removeProxyNames(proxies, names) {
  var out = [];
  for (var i = 0; i < proxies.length; i++) {
    var p = proxies[i];
    if (p && p.name && names[p.name]) continue;
    out.push(p);
  }
  return out;
}

function resetSiiProxy(config) {
  config.proxies = removeProxyNames(ensureArray(config.proxies), SII_MANAGED_PROXY_NAMES);
  config["proxy-groups"] = ensureArray(config["proxy-groups"]).filter(function(group) {
    return group && group.name !== SII_PROXY_GROUP_NAME;
  });
}

function injectSiiProxy(config) {
  config.proxies = ensureArray(config.proxies);
  for (var i = 0; i < SII_PROXIES.length; i++) {
    config.proxies = upsertProxy(config.proxies, SII_PROXIES[i]);
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

验证以下三项：

```bash
lsof -iTCP:<mixed-port> -sTCP:LISTEN
curl -sS -o /dev/null -w "sii: %{http_code}\n" -x http://127.0.0.1:<mixed-port> https://qz.sii.edu.cn
inspire config check
```

`qz.sii.edu.cn` 访问失败时，依次确认 Clash Verge 规则包含 `DOMAIN-SUFFIX,sii.edu.cn,SII Proxy`、`SII Proxy` 组选择了可用节点或 `DIRECT`，再用 `inspire config show --compact --filter Proxy` 检查账号级 proxy、Shell proxy、有效路由和 `NO_PROXY` 匹配结果。
