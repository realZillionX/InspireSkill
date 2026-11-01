#!/usr/bin/env python3
"""pick-zone: 启智排卡选区决策工具(补充 inspire CLI,只做推荐不做提交)。

用法:
  python pick-zone.py --gpus 8                 # 8卡整节点任务,推荐最空的组
  python pick-zone.py --gpus 1 --gpu-type H100 # 单卡碎卡,只看 H100
  python pick-zone.py --gpus 32 --gpu-type H200
  python pick-zone.py --gpus 4 --json          # 结构化输出

规则(编码自平台 2026-07-04 官方通知与实测):
  - 1/2/4 卡 = 碎卡通道:高优只能去开发区组(训练区碎卡只收低优)
  - 8 卡及以上(8 的倍数)= 整节点通道:开发区/训练区均可,每实例固定 8 卡
  - 碎卡得分 = high_priority_available_gpus(空闲保障 + 可抢占低优)
  - 整节点得分 = (free_nodes + high_priority_free_nodes) * gpus_per_node
  - 只推荐得分 ≥ 需求 的组;全部不满足时明确报"无可用组",禁止推荐零分组死磕
"""
import argparse
import json
import subprocess
import sys
import os

INSPIRE = os.path.join(os.path.expanduser("~"), ".local", "bin", "inspire.exe")
if not os.path.exists(INSPIRE):
    INSPIRE = "inspire"

FRAGMENT_SIZES = {1, 2, 4}


def fetch_groups(workspace, account=None):
    cmd = [INSPIRE, "--json", "resources", "availability", "--workspace", workspace]
    if account:
        cmd += ["--account", account]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    s = r.stdout + "\n" + r.stderr
    i = s.find("{")
    if i < 0:
        raise SystemExit(f"ERROR: inspire 输出无法解析: {s[:300]}")
    d = json.loads(s[i:])
    if d.get("success") is False:
        err = d.get("error") or {}
        raise SystemExit(f"ERROR: 平台返回错误 [{err.get('type')}]: {err.get('message')}")
    items = d.get("data", {}).get("items", [])
    return [g for g in items if g.get("kind") == "gpu" and g.get("total_gpus", 0) > 0]


def zone_of(name):
    if name.startswith("开发区"):
        return "开发区"
    if name.startswith("训练区"):
        return "训练区"
    return "其他"


def main():
    ap = argparse.ArgumentParser(description="启智排卡选区推荐")
    ap.add_argument("--gpus", type=int, required=True, help="需要的 GPU 总数(1/2/4/8/16/...)")
    ap.add_argument("--gpu-type", default=None, help="过滤卡型:H100 / H200 / 4090 / 910B 等(可选)")
    ap.add_argument("--workspace", default="分布式训练空间")
    ap.add_argument("--priority", type=int, default=4, choices=[1, 4], help="默认 4(高档)")
    ap.add_argument("--account", default=None)
    ap.add_argument("--top", type=int, default=3, help="展示前 N 个候选")
    ap.add_argument("--json", action="store_true", help="结构化输出")
    a = ap.parse_args()

    n = a.gpus
    if n in FRAGMENT_SIZES:
        channel = "fragment"
    elif n >= 8 and n % 8 == 0:
        channel = "whole_node"
    else:
        print(f"ERROR: --gpus {n} 无效。碎卡只支持 1/2/4,整节点须为 8 的倍数(平台 quota 形状)。")
        sys.exit(2)

    groups = fetch_groups(a.workspace, a.account)
    rows = []
    for g in groups:
        name = g["compute_group"]
        gtype = g.get("gpu_type", "")
        if a.gpu_type and a.gpu_type.upper() not in gtype.upper():
            continue
        zone = zone_of(name)
        if channel == "fragment":
            # 高优碎卡只能去开发区;低优碎卡任意(训练区碎卡只收低优)
            eligible = True if a.priority == 1 else (zone != "训练区")
            score = g.get("high_priority_available_gpus", 0) or 0
        else:
            eligible = True  # 整节点两区均可
            per = g.get("gpus_per_node", 8) or 8
            score = ((g.get("free_nodes", 0) or 0) +
                     (g.get("high_priority_free_nodes", 0) or 0)) * per
        rows.append({
            "group": name, "gpu_type": gtype, "zone": zone,
            "score": score, "eligible": eligible,
            "free_nodes": g.get("free_nodes", 0),
            "preemptable_nodes": g.get("high_priority_free_nodes", 0),
            "available_gpus": g.get("available_gpus", 0),
            "high_pri_gpus": g.get("high_priority_available_gpus", 0),
            "ready_nodes": g.get("ready_nodes", 0),
            "total_nodes": g.get("total_nodes", 0),
        })

    rows.sort(key=lambda r: (r["eligible"], r["score"]), reverse=True)
    qualified = [r for r in rows if r["eligible"] and r["score"] >= n]

    if a.json:
        print(json.dumps({
            "request": {"gpus": n, "gpu_type": a.gpu_type, "priority": a.priority,
                        "channel": channel, "workspace": a.workspace},
            "recommended": qualified[0] if qualified else None,
            "candidates": rows[:max(a.top, len(qualified) or a.top)],
            "qualified_count": len(qualified),
        }, ensure_ascii=False, indent=1))
        return

    ch_desc = "碎卡(1-4卡)" if channel == "fragment" else f"整节点({n//8}节点×8卡)"
    print(f"需求:{n} 卡 | 通道:{ch_desc} | 优先级:{a.priority} | 卡型:{a.gpu_type or '不限'}")
    print(f"{'组':<32}{'区':<6}{'得分':>6}  {'空节点':>4}{'可抢节点':>6}  {'碎卡可用':>8}  适用")
    for r in rows[:a.top]:
        ok = "✓" if (r["eligible"] and r["score"] >= n) else ("✗规格" if not r["eligible"] else "✗不足")
        print(f"{r['group']:<32}{r['zone']:<6}{r['score']:>6}  {r['free_nodes']:>4}{r['preemptable_nodes']:>6}"
              f"  {r['high_pri_gpus']:>8}  {ok}")
    print()
    if qualified:
        best = qualified[0]
        margin = best["score"] - n
        print(f"推荐:{best['group']}({best['gpu_type']},得分 {best['score']},余量 {margin})")
        print(f"下一步:inspire job quota --workspace {a.workspace} --group {best['group']}  # 取同组 quota 行")
        print(f"      然后 job create 显式传 --group {best['group']} + 对应 --quota, dry-run 后提交,并登记 ~/.inspire/JOBS.md")
        if margin == 0:
            print("注意:余量为 0,提交后可能需抢占等待;备选组也在上表,首选 10 分钟无进展即换组。")
    else:
        print("无满足需求的组(得分全部不足)。不要在单一组重试!选项:①降规格(8卡→4卡) ②换卡型 "
              "③低优(priority 1,可占碎片容量) ④稍后再查。")
        sys.exit(1)


if __name__ == "__main__":
    main()
