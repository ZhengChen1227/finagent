"""网络边界测试：服务只听本机，运行期不得有非 DeepSeek 的对外请求。

本项目的核心承诺之一是"封闭数据环境"：所有财务数字都从用户上传的
PDF 里抽出来，运行期只允许访问 DeepSeek 这一家外部服务。
承诺要靠机器验证，因此这里做三件事：

1. 源码静态检查——不能监听 0.0.0.0、不能有 --host 参数、
   不能出现任何已删除的取数源或外部行情域名。
2. 真实监听检查——起真服务，确认只有 127.0.0.1 能连上，
   局域网地址连不上（每人在自己机器上独立运行，不需要同一网段）。
3. 运行期审计——用 Python 的审计钩子记录整条确定性链路里的
   每一次 DNS 解析与连接，确认除本机外没有任何对外请求。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import _fixtures as F

RESULTS = []
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 允许联网的域名：唯一的一家大模型服务。任何其它域名都是违规。
ALLOWED_HOSTS = {"api.deepseek.com"}
# 已删除的取数源与常见行情站点：出现在源码里就说明有人又把联网取数接回来了。
BANNED = ("eastmoney", "push2his", "cninfo", "akshare", "tushare", "baostock",
          "sina.com", "sinajs", "10jqka", "xueqiu", "hexun", "jrj.com",
          "eastmoney.com", "东方财富", "巨潮", "同花顺", "新浪财经")


def check(title: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((title, bool(ok)))
    print(f"  [{'通过' if ok else '失败'}] {title}" + (f"  {detail}" if detail else ""))


# ------------------------------------------------------------------ 1 静态


def static_checks() -> None:
    import app.server as srv

    check("服务地址硬编码为 127.0.0.1", srv.HOST == "127.0.0.1", srv.HOST)
    source = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    check("源码里没有 0.0.0.0", "0.0.0.0" not in source)
    # 只检查"有没有注册这个命令行参数"，不检查字样：文档里会说明
    # "本模块不提供 --host"，那是设计声明，不是可用的开关。
    check("没有 --host 参数（不给用户改监听地址的机会）",
          'add_argument("--host"' not in source
          and "add_argument('--host'" not in source)
    check("没有局域网共享模式的残留",
          not any(w in source for w in ("local_addresses", "share", "局域网共享")),
          "")
    check("模型接口固定为 DeepSeek",
          srv.BASE_URL.startswith("https://api.deepseek.com/"), srv.BASE_URL)

    # 源码里不得出现已删除的取数源或行情域名
    hits = []
    targets = [ROOT / "run.py", ROOT / "app" / "server.py", ROOT / "app" / "pdfexport.py"]
    targets += sorted((ROOT / "finagent").rglob("*.py"))
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for word in BANNED:
            if word in text:
                hits.append(f"{path.relative_to(ROOT)}:{word}")
    check("源码中不含任何被删除的联网取数源", not hits, "、".join(hits[:6]))

    # 唯一允许的出网域名必须写在配置里，便于评审一眼看到
    from finagent.config import ALLOWED_HOST

    check("配置里声明了唯一允许的出网域名", ALLOWED_HOST in ALLOWED_HOSTS, ALLOWED_HOST)


def host_arg_rejected() -> None:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "app/server.py", "--host", "0.0.0.0", "--port", "0"],
        cwd=ROOT, env=CHILD_ENV, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    ok = proc.returncode == 2 and "--host" in (proc.stderr or "")
    check("命令行传 --host 会被拒绝", ok, f"退出码 {proc.returncode}")


# ------------------------------------------------------------------ 2 监听


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def lan_ips() -> list:
    ips = []
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips = [a for a in addrs if not a.startswith("127.")]
    except OSError:
        pass
    return ips


def binding_checks() -> None:
    port = free_port()
    env = dict(CHILD_ENV, FINAGENT_NO_BROWSER="1")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "app/server.py", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    try:
        ok = False
        for _ in range(40):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status",
                                            timeout=5) as resp:
                    ok = resp.status == 200
                break
            except Exception:
                time.sleep(0.25)
        check("服务在本机 127.0.0.1 可访问", ok, f"端口 {port}")

        ips = lan_ips()
        if not ips:
            print("  [跳过] 局域网地址连不上：本机没有非回环地址可测。")
        else:
            reachable = []
            for ip in ips[:2]:
                try:
                    with socket.create_connection((ip, port), timeout=2):
                        reachable.append(ip)
                except OSError:
                    pass
            check("局域网地址连不上（不需要同一网段，也不需要主机开机）",
                  not reachable, f"可连：{reachable}" if reachable else f"试了 {ips[:2]}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ------------------------------------------------------------------ 3 运行期审计


def audit_probe() -> int:
    """子进程：装审计钩子跑一遍确定性链路，打印记录到的外部目标。"""
    seen: list = []

    def hook(event, args):
        if event == "socket.getaddrinfo":
            seen.append(("getaddrinfo", str(args[0])))
        elif event == "socket.connect":
            target = args[1]
            host = target[0] if isinstance(target, (tuple, list)) and target else target
            seen.append(("connect", str(host)))

    sys.addaudithook(hook)

    from finagent.config import load_config, uploads_dir
    from finagent.ingest.dataset import UploadSet
    from finagent.metrics import indicators as I
    from finagent.validate.anomaly import run_rules

    cfg = load_config()
    us = UploadSet(run_id="_net", root=uploads_dir(cfg))
    for path in F.pick("002714", ("semi",)):
        us.add(path)
    merged = us.merged("002714")
    table, _ = I.compute_all(merged["frames"])
    findings = run_rules("002714", merged["frames"], table, cfg, since_year=2025)
    from finagent.agent.tools import build_tools

    reg = build_tools(cfg, None, uploads=us, focus_key="002714")
    reg["list_materials"].handler(None)
    reg["search_disclosure"].handler("非经常性损益", None, 2)
    print("AUDIT:" + json.dumps({"seen": seen, "findings": len(findings)}))
    return 0


def runtime_checks() -> None:
    if F.corpus_dir("002714") is None:
        print("  [跳过] 运行期出网审计：" + F.missing_message("002714"))
        return
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", __file__, "--audit"],
        cwd=ROOT, env=CHILD_ENV, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900)
    line = ""
    for text in (proc.stdout or "").splitlines():
        if text.startswith("AUDIT:"):
            line = text[len("AUDIT:"):]
    if not line:
        check("运行期出网审计可运行", False, (proc.stderr or "")[-200:])
        return
    payload = json.loads(line)
    targets = [t for _, t in payload["seen"]]
    bad = sorted({t for t in targets
                  if t not in ALLOWED_HOSTS
                  and not t.startswith("127.")
                  and t not in ("localhost", "", "None")})
    check("确定性链路全程没有对外请求",
          not bad, "出现的非本机目标：" + str(bad) if bad else
          f"记录 {len(targets)} 次地址解析／连接，均为本机或空")
    check("审计期间仍正常产出结论", payload.get("findings", 0) > 0,
          f"{payload.get('findings')} 条")


def main() -> int:
    if "--audit" in sys.argv:
        return audit_probe()
    print("=" * 74)
    print("网络边界测试")
    print("=" * 74)
    static_checks()
    host_arg_rejected()
    binding_checks()
    runtime_checks()
    print("=" * 74)
    bad = [t for t, ok in RESULTS if not ok]
    print(f"网络边界测试：{len(RESULTS) - len(bad)}/{len(RESULTS)} 项通过")
    for title in bad:
        print("  未通过：" + title)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
