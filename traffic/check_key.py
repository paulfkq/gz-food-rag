"""腾讯位置服务 Key 自检：一次性判断 key / 权限 / 白名单是否配好。

用法（Git Bash / PowerShell 均可）：
    export TENCENT_MAP_KEY='你的key'      # PowerShell: $env:TENCENT_MAP_KEY='你的key'
    python -m traffic.check_key

它会依次实测 5 类接口，并把每个 status 翻译成"下一步该做什么"：
    IP 定位（同时拿到本机出口公网 IP，直接拿去填白名单）
    驾车规划 / 公交规划 / 距离矩阵 / 地址解析

设计约束：
    - key 只从环境变量读；日志/输出只显示脱敏前缀。
    - httpx(trust_env=False)：绕过系统代理，和 traffic.providers.tencent 的实际调用路径一致
      （否则你看到的是代理出口 IP，填进白名单会不对）。
    - 不修改任何项目状态；纯只读探测。
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlencode

import httpx

BASE = "https://apis.map.qq.com"
FROM = "23.1347,113.3230"   # 体育西路（GCJ-02）
TO = "23.1066,113.3245"     # 广州塔（GCJ-02）

# 腾讯 WebService 状态码 → 中文诊断（常见项；完整表见官方 status 文档）
DIAG = {
    0:   "OK",
    110: "请求来源未被授权：检查 Key 的域名白名单/授权 IP 是否填了本机出口公网 IP",
    111: "签名验证失败：本模块不用 SK 签名，若出现说明控制台开了签名校验，改用无签名调用或补 SK",
    112: "IP 未被授权：把脚本末尾打印的出口公网 IP 填进 Key 的授权 IP",
    113: "此功能未被授权：去控制台『配额管理』给该接口单独申请开通（不是勾选 WebServiceAPI 就够）",
    120: "QPS 超限：稍等 1-2 秒重试，或降低调用频率",
    121: "日调用量达上限（新 Key 常见：该接口默认日配额为 0，并非真的被调用完）："
         "控制台 → 配额管理 → 账户额度/申请配额，给该接口分配日调用次数",
    190: "无效 Key：确认 Key 未被删除/停用",
    199: "此 Key 未开启 WebService：回到 Key 编辑页勾选 WebServiceAPI 并保存",
    300: "缺少必要字段：检查参数拼接",
    301: "缺少 key 参数",
    310: "参数格式错误：检查坐标格式（lat,lng，GCJ-02）",
    311: "Key 格式错误：检查是否复制完整（腾讯 key 形如 XXXX-XXXX-...）",
    320: "参数类型错误",
    326: "起终点距离过近：换一组测试坐标",
    375: "局域网 IP 无法定位：IP 定位只支持公网 IP，属正常兜底失败",
    500: "后端超时：稍后重试",
}


def redact(key: str) -> str:
    """只显示前 8 位与长度，绝不回显完整 key。"""
    return f"{key[:8]}...（len={len(key)}）" if key else "<空>"


def _get(path: str, params: dict, timeout: float = 8.0) -> dict:
    url = BASE + path + "?" + urlencode(params)
    try:
        resp = httpx.get(url, timeout=timeout, trust_env=False)  # 关键：不走代理
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"status": -1, "message": f"{type(e).__name__}: {e}"}


def run_checks(key: str) -> tuple[list[dict], str | None]:
    checks = [
        ("IP 定位",      "/ws/location/v1/ip", {"key": key}),
        ("驾车规划",      "/ws/direction/v1/driving",
         {"key": key, "from": FROM, "to": TO}),
        ("公交规划",      "/ws/direction/v1/transit",
         {"key": key, "from": FROM, "to": TO}),
        ("距离矩阵",      "/ws/distance/v1/matrix",
         {"key": key, "mode": "driving", "from": FROM, "to": TO}),
        ("地址解析",      "/ws/geocoder/v1/",
         {"key": key, "address": "广州市天河区体育西路", "region": "广州"}),
    ]
    results: list[dict] = []
    egress_ip: str | None = None
    for name, path, params in checks:
        data = _get(path, params)
        st = data.get("status")
        msg = data.get("message", "")
        diag = DIAG.get(st, "未收录状态：查 status 文档或提工单")
        results.append({"check": name, "status": st, "message": msg, "diagnosis": diag})
        if name == "IP 定位" and st == 0:
            result = data.get("result") or {}
            egress_ip = result.get("ip")
            addr = (result.get("ad_info") or {})
            if egress_ip:
                results[0]["diagnosis"] += (
                    f"；本机出口公网 IP = {egress_ip}"
                    f"（{addr.get('province', '')}{addr.get('city', '')}）"
                )
    return results, egress_ip


def main() -> int:
    key = os.environ.get("TENCENT_MAP_KEY", "").strip()
    as_json = "--json" in sys.argv
    if not key:
        print("未检测到 TENCENT_MAP_KEY 环境变量。请先设置：")
        print("  Git Bash : export TENCENT_MAP_KEY='你的key'")
        print("  PowerShell: $env:TENCENT_MAP_KEY='你的key'")
        return 2

    if not as_json:
        print(f"[check_key] 使用 key {redact(key)}；直连 apis.map.qq.com（绕过系统代理）\n")

    results, egress_ip = run_checks(key)

    if as_json:
        print(json.dumps({"key": redact(key), "results": results, "egress_ip": egress_ip},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = "OK " if r["status"] == 0 else "FAIL"
            print(f"[{mark}] {r['check']:<8} status={r['status']}  {r['message']}")
            if r["status"] != 0:
                print(f"         -> {r['diagnosis']}")
        print("\n结论：")
        oks = [r["check"] for r in results if r["status"] == 0]
        quota_fail = [r for r in results if r["status"] == 121]
        if len(oks) == len(results):
            print("  全部通过。把下面这个 IP 填进 Key 的授权 IP 再保存一次（收紧安全）。")
        else:
            print(f"  通过 {len(oks)}/{len(results)}：{'、'.join(oks) or '无'}。"
                  "按上面每条 -> 提示逐项修复后重跑本脚本。")
            if quota_fail and oks:
                print("  注意：仅部分接口报 121，说明 key 有效、白名单也通；"
                      "这些接口在新 Key 上的默认日配额通常为 0，并非真的被调用完。")
                print("  修复：控制台 → 配额管理 → 账户额度（或申请配额），"
                      f"给「{'、'.join(r['check'] for r in quota_fail)}」分配日调用次数后重跑。")
        if egress_ip:
            print(f"  本机出口公网 IP（填白名单用）：{egress_ip}")
        else:
            print("  未能拿到出口公网 IP（IP 定位失败时属正常）："
                  "可另开终端执行 curl -s https://api.ipify.org 获取。")

    # 全部通过返回 0，否则 1（方便脚本化）
    return 0 if all(r["status"] == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
