# Windows 原生

只在用户的本机是 Windows 时加载这一份。安装、更新、账号等通用流程在 [`install-and-config.md`](install-and-config.md)；本文只放 Windows 特有的部分。不需要 WSL。

## 安装

前置：Python 3.10+、`uv`（推荐）或 `pipx`、Windows OpenSSH 客户端。

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

`install.ps1` 和 `install.sh` 装的是同一个 PyPI 包，走 `uv tool` / `pipx`。参数 `-SkipPlaywright`（跳过 Chromium 下载，浏览器登录不可用）、`-SkipSkill`（只装 CLI）、`-Version <x.y.z>`（装指定版本）。harness 由 CLI 自动探测，没有 `--harness` 的对应参数。

macOS 的每日 launchd 版本检查在 Windows 上没有对应物，不影响使用：CLI 每次运行时会在缓存过期后自己拉起一次后台检查。更新、卸载与其他平台相同（`inspire update` / `inspire uninstall`），`--purge-runtime` 在 Windows 上清的是 `%LOCALAPPDATA%\ms-playwright`。

## 四件 Windows 特有的事

| 项 | 事实 | 读错的后果 |
| --- | --- | --- |
| `ssh.exe` 来源 | 系统自带的和 Git for Windows 附带的对 ProxyCommand 的处理不同：系统版不经过 shell，Git 版经过 `/bin/sh` | 两个混用时报错信息互相矛盾。用 `Get-Command ssh -All` 确认排在最前的是哪个 |
| `ssh-config` 输出重定向 | Windows PowerShell 5.1 的 `>` / `>>` 写 UTF-16LE | OpenSSH 读不了这个 config。用 `\| Out-File -Encoding utf8 -Append`；PowerShell 7+ 默认 UTF-8，`>>` 可用 |
| 私钥权限 | Windows OpenSSH 会拒绝 ACL 过宽的私钥 | 域账号或有继承 ACE 的 profile 下 `notebook ssh` 报 UNPROTECTED PRIVATE KEY FILE。用文件属性 → 安全，把 `%USERPROFILE%\.ssh` 收敛到只有本人 |
| 文件传输 | `rsync` 不随 Windows 提供 | 用 `inspire notebook scp`。本地路径直接写盘符形式（`C:\...`）即可，Windows 版 scp 认盘符 |
