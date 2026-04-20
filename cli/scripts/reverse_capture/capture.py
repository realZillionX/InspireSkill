"""Browser API capture tool — drive the Inspire frontend with Playwright,
intercept every `/api/v1/*` request/response, write JSONL.

End-to-end flow:
1. Log in via CAS → Keycloak → qz.sii.edu.cn (or reuse a cached `storage_state`).
2. Attach request/response listeners to the browser context.
3. Systematically navigate candidate frontend routes (notebook list, train list,
   HPC list, model library, ...), scroll, switch tabs, click first table rows,
   open `+ 新建` modals (then ESC without submitting).
4. Dump every captured XHR to JSONL for post-hoc analysis.

Read-only by design: the click policy explicitly skips buttons whose text
matches `删除 / 停止 / 保存 / 提交 / 确认 / Delete / Stop / ...`.

Usage:
    # First time (will open a Keycloak login via Playwright).
    INSPIRE_USERNAME=xxx INSPIRE_PASSWORD=xxx \\
        uv run python scripts/reverse_capture/capture.py --out /tmp/bapi.jsonl

    # Reuse an existing session (e.g. from InspireSkill's CLI session cache).
    uv run python scripts/reverse_capture/capture.py \\
        --storage-state ~/.cache/inspire-skill/web_session-<user>.json \\
        --out /tmp/bapi.jsonl

Credential resolution order:
    1. --username / --password CLI flags
    2. INSPIRE_USERNAME / INSPIRE_PASSWORD env
    3. `[auth]` block in ~/.config/inspire/config.toml

Subsequent runs can skip login entirely by pointing `--storage-state` at the
`storage_state.json` that this script saves next to `--out`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    from playwright.sync_api import (
        BrowserContext,
        Page,
        Request,
        Response,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ModuleNotFoundError:
    sys.exit("playwright not installed; `uv add playwright && playwright install chromium`")

BASE = "https://qz.sii.edu.cn"
API_PREFIX = "/api/v1/"
DEFAULT_PROXY = "http://127.0.0.1:7897"


FORBIDDEN_CLICK = re.compile(
    r"(删除|停止|关闭|保存|提交|注销|退出|登出|绑定|发送|下载|上传|重启|重置|续费|确认|确定|同意"
    r"|Delete|Stop|Save|Submit|Remove|Logout|Upload|Download|Restart|Reset|Renew|Confirm|Agree"
    r"|\bOK\b)",  # word-boundary on OK so it doesn't swallow "Notebook" / "Stopped"-style labels
    re.I,
)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _resolve_credentials(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    user = args.username or os.environ.get("INSPIRE_USERNAME")
    pw = args.password or os.environ.get("INSPIRE_PASSWORD")
    if user and pw:
        return user, pw
    cfg = Path.home() / ".config/inspire/config.toml"
    if cfg.exists():
        text = cfg.read_text()
        if not user:
            m = re.search(r'username\s*=\s*"([^"]+)"', text)
            if m:
                user = m.group(1)
        if not pw:
            m = re.search(r'password\s*=\s*"([^"]+)"', text)
            if m:
                pw = m.group(1)
    return user, pw


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def make_logger(log_file: Optional[Path]):
    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if log_file is not None:
            with log_file.open("a") as f:
                f.write(line + "\n")

    return log


def _truncate(s: Optional[str], n: int = 8000) -> Optional[str]:
    if s is None:
        return None
    return s if len(s) <= n else s[:n] + f"...[+{len(s) - n}]"


# ---------------------------------------------------------------------------
# CAS + Keycloak login
# ---------------------------------------------------------------------------


def cas_login(context: BrowserContext, username: str, password: str, log) -> None:
    """Drive the CAS login form. Leaves a logged-in session in the context."""
    page = context.new_page()
    log("CAS login: goto /login")
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)

    # The CAS form re-renders after its bundle loads; poll for inputs.
    for i in range(30):
        page.wait_for_timeout(1000)
        try:
            ready = page.evaluate(
                "() => !!(document.querySelector('section.conactive #username') "
                "&& document.querySelector('section.conactive #passwordShow'))"
            )
            if ready:
                log(f"  CAS form ready after {i + 1}s")
                break
        except Exception:
            pass
    else:
        raise RuntimeError("CAS form never rendered")

    # Also wait for the CAS JS (security.js + login.js) that defines
    # checkPassLogin. This poll has a hard cap — if the bundle fails to load
    # within ~10s the login is doomed, surface it now rather than let the
    # next `passbutton.click()` silently send an unencrypted password to CAS.
    checkpass_ready = False
    for _ in range(20):
        page.wait_for_timeout(500)
        try:
            if page.evaluate("() => typeof checkPassLogin === 'function'"):
                checkpass_ready = True
                break
        except Exception:
            pass
    if not checkpass_ready:
        raise RuntimeError(
            "CAS script `checkPassLogin` never defined within 10s; login.js "
            "may have failed to load (check proxy / network / CSP)"
        )

    # The inputs are CSS-hidden; Playwright's fill() won't work. Set values via
    # JS scoped to the active section and dispatch input/change events.
    page.evaluate(
        f"""() => {{
            const scope = document.querySelector('section.conactive');
            const u = scope.querySelector('#username');
            const p = scope.querySelector('#passwordShow');
            u.value = {username!r};
            u.dispatchEvent(new Event('input', {{bubbles:true}}));
            u.dispatchEvent(new Event('change', {{bubbles:true}}));
            p.value = {password!r};
            p.dispatchEvent(new Event('input', {{bubbles:true}}));
            p.dispatchEvent(new Event('change', {{bubbles:true}}));
        }}"""
    )
    page.wait_for_timeout(500)

    # Click the submit button natively. onclick=checkPassLogin() encrypts the
    # password via RSA before the form actually POSTs.
    page.evaluate(
        "() => { document.querySelector('section.conactive #passbutton').click(); }"
    )

    # Wait for the redirect chain (CAS → Keycloak broker → qz) to complete.
    for i in range(60):
        page.wait_for_timeout(1000)
        if urlparse(page.url).netloc == "qz.sii.edu.cn":
            log(f"  logged in after {i + 1}s: {page.url}")
            break
    else:
        raise RuntimeError(f"login never returned to qz; stuck at {page.url}")

    # Confirm API-level auth works.
    for attempt in range(20):
        try:
            r = context.request.get(
                f"{BASE}/api/v1/user/detail",
                headers={
                    "Accept": "application/json",
                    "Referer": f"{BASE}/jobs/distributedTraining",
                },
                timeout=10000,
            )
            if r.status == 200:
                log(f"  /user/detail 200 after {attempt + 1} polls")
                page.close()
                return
        except Exception:
            pass
        time.sleep(1.5)
    raise RuntimeError("login completed in browser but /user/detail still 401")


# ---------------------------------------------------------------------------
# Network capture
# ---------------------------------------------------------------------------


def install_listeners(context: BrowserContext, sink: list[dict]) -> None:
    pending: dict[int, dict] = {}

    def on_req(r: Request) -> None:
        parsed = urlparse(r.url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if API_PREFIX not in path:
            return
        try:
            body = r.post_data
        except Exception:
            body = None
        pending[id(r)] = {
            "ts": time.time(),
            "method": r.method,
            "url": r.url,
            "path": path,
            "referer": r.headers.get("referer"),
            "content_type": r.headers.get("content-type"),
            "request_body": _truncate(body),
            "resource_type": r.resource_type,
        }

    def on_resp(resp: Response) -> None:
        rec = pending.pop(id(resp.request), None)
        if rec is None:
            return
        rec["status"] = resp.status
        try:
            rec["response_body"] = _truncate(resp.text())
        except Exception as e:
            rec["response_body"] = f"<unreadable:{e!r}>"
        sink.append(rec)

    def on_failed(r: Request) -> None:
        rec = pending.pop(id(r), None)
        if rec is None:
            return
        rec["status"] = None
        rec["failure"] = r.failure
        sink.append(rec)

    context.on("request", on_req)
    context.on("response", on_resp)
    context.on("requestfailed", on_failed)


# ---------------------------------------------------------------------------
# Navigation + read-only interaction
# ---------------------------------------------------------------------------


def settle(page: Page, idle: int = 10_000, sleep: float = 1.5) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=idle)
    except PlaywrightTimeoutError:
        pass
    time.sleep(sleep)


def goto(page: Page, url: str, label: str, log) -> None:
    log(f"→ {label}: {urlparse(url).path[:70]}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        log("  dom timeout")
    settle(page, idle=12_000, sleep=1.5)


def scroll(page: Page, times: int = 2) -> None:
    for _ in range(times):
        try:
            page.mouse.wheel(0, 1500)
        except Exception:
            pass
        time.sleep(0.4)


def click_first_row(page: Page, log) -> bool:
    for sel in [
        ".ant-table-tbody tr:not(.ant-table-placeholder) a[href]",
        ".ant-table-tbody tr.ant-table-row:not(.ant-table-placeholder) td:first-child",
        ".ant-table-tbody tr.ant-table-row:not(.ant-table-placeholder)",
        "tbody tr:first-child a",
    ]:
        try:
            el = page.locator(sel).first
            if el.count() == 0:
                continue
            txt = (el.inner_text(timeout=500) or "").strip()
            if not txt or FORBIDDEN_CLICK.search(txt[:60]):
                continue
            log(f"  row: '{txt[:35]}'")
            el.click(timeout=3000, no_wait_after=True)
            settle(page, idle=8000, sleep=2.0)
            return True
        except Exception:
            continue
    return False


def open_and_close_modal(page: Page, log) -> None:
    openers = [
        "button:has-text('+ 新建')",
        "button:has-text('新建交互式建模')",
        "button:has-text('新建训练任务')",
        "button:has-text('部署服务')",
        ".ant-btn-primary:has-text('新建')",
        ".ant-btn-primary:has-text('创建')",
    ]
    for sel in openers:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            txt = (btn.inner_text(timeout=400) or "").strip()
            if FORBIDDEN_CLICK.search(txt):
                continue
            log(f"  modal: '{txt[:30]}'")
            btn.click(timeout=3000, no_wait_after=True)
            settle(page, idle=12_000, sleep=3.0)
            # Click any select-like fields inside the modal to trigger lazy XHRs.
            sels = page.locator(
                ".ant-drawer-body .ant-select-selector, .ant-modal-body .ant-select-selector"
            )
            for i in range(min(sels.count(), 6)):
                try:
                    e = sels.nth(i)
                    if not e.is_visible():
                        continue
                    e.click(timeout=1500, no_wait_after=True)
                    settle(page, idle=4000, sleep=1.0)
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                except Exception:
                    continue
            # Close the modal.
            page.keyboard.press("Escape")
            time.sleep(0.8)
            for close_sel in [".ant-modal-close", ".ant-drawer-close"]:
                try:
                    c = page.locator(close_sel).first
                    if c.count() > 0:
                        c.click(timeout=1000, no_wait_after=True)
                        break
                except Exception:
                    continue
            return
        except Exception:
            continue


DEFAULT_ROUTES: list[tuple[str, str]] = [
    ("home", "/"),
    ("dashboard", "/dashboard"),
    ("train_list", "/jobs/distributedTraining"),
    ("hpc_list", "/jobs/highPerformanceComputing"),
    ("notebook_list", "/jobs/interactiveModeling"),
    ("inference_list", "/jobs/modelDeployment"),
    ("projects", "/projects"),
    ("resources", "/resources"),
    ("userCenter", "/userCenter"),
]


def sweep(page: Page, workspace_id: Optional[str], log) -> None:
    if workspace_id:
        try:
            page.evaluate(f"() => localStorage.setItem('spaceId', {workspace_id!r})")
            log(f"localStorage.spaceId = {workspace_id}")
        except Exception:
            pass

    for label, path in DEFAULT_ROUTES:
        try:
            goto(page, f"{BASE}{path}", label, log)
            scroll(page, times=2)
            click_first_row(page, log)
            if urlparse(page.url).path != path:
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=10_000)
                    settle(page, idle=5000, sleep=1.0)
                except Exception:
                    pass
            open_and_close_modal(page, log)
        except Exception as e:
            log(f"  {label} err: {e!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/tmp/bapi.jsonl", help="JSONL output path")
    ap.add_argument(
        "--storage-state",
        default=None,
        help="Path to a saved Playwright storage_state (skips login)",
    )
    ap.add_argument(
        "--save-storage-state",
        default=None,
        help="After login, save the storage_state to this path for reuse",
    )
    ap.add_argument("--proxy", default=os.environ.get("INSPIRE_PLAYWRIGHT_PROXY", DEFAULT_PROXY))
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--workspace", default=None, help="localStorage.spaceId for the sweep")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--log", default=None, help="Write log mirror to this file")
    args = ap.parse_args()

    log = make_logger(Path(args.log) if args.log else None)
    out_jsonl = Path(args.out)
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    sink: list[dict] = []

    with sync_playwright() as p:
        proxy_kw = {"proxy": {"server": args.proxy}} if args.proxy else {}
        browser = p.chromium.launch(headless=not args.headed, **proxy_kw)

        ctx_kw: dict = {
            "ignore_https_errors": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if args.storage_state:
            ctx_kw["storage_state"] = json.loads(Path(args.storage_state).read_text())

        context = browser.new_context(**ctx_kw)

        if not args.storage_state:
            user, pw = _resolve_credentials(args)
            if not (user and pw):
                sys.exit(
                    "no credentials: pass --username/--password, set INSPIRE_USERNAME/PASSWORD, "
                    "or populate ~/.config/inspire/config.toml"
                )
            try:
                cas_login(context, user, pw, log)
            except RuntimeError as exc:
                # cas_login raises RuntimeError for well-understood failure
                # modes (form never rendered, checkPassLogin undefined, login
                # never returned to qz). Fail with a concise message rather
                # than dumping a full Playwright traceback.
                log(f"CAS login failed: {exc}")
                context.close()
                browser.close()
                sys.exit(f"CAS login failed: {exc}")

            if args.save_storage_state:
                Path(args.save_storage_state).write_text(json.dumps(context.storage_state()))
                log(f"saved storage_state → {args.save_storage_state}")

        install_listeners(context, sink)
        page = context.new_page()
        sweep(page, args.workspace, log)

        time.sleep(3)
        log(f"total captured: {len(sink)}")
        context.close()
        browser.close()

    with out_jsonl.open("w") as f:
        for rec in sink:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"wrote {out_jsonl} ({out_jsonl.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
