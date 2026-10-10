# mk/package_split.py
"""把 install/ 目录拆成三个分发包（打包拆分）。

背景：agent 可执行文件只冻结第三方依赖（bootstrap 设计），
一方 Python 代码以源码形式分发。整包 100+ MiB 里大部分
（GUI 运行时 / PyInstaller 产物）极少变化，不应每次构建
都让用户重新下载。

产出（目录输出到 --out 下，由 CI 分别作为 artifact 上传——
GitHub artifact 下载本身就是 zip，这里**不再打内层 zip**，
用户下载后解压一层即可把内容直接覆盖进包根目录）：
  base/        GUI + runtimes + tools + docs
               （MFAAvalonia 版本变化才需要重新下载）
  agent-rt/    agent/dist（exe + _internal）+ agent/agent.conf
               （第三方依赖变化才需要重新下载）
  agent/       agent/src + resource + resource_en + interface.json
               （每次构建；Python 侧改动的最小下载单元）

用法：
  python mk/package_split.py --install install --out split \
      --tag v1.2.3 --arch x86_64 --mfa-tag v2.10.8 --depskey abc12345
"""
import argparse
import shutil
import sys
from pathlib import Path

# 属于"每次构建"件的内容
AGENT_PKG_PARTS = ("agent", "resource", "resource_en", "interface.json")


def copy_dir(src: Path, out: Path, predicate) -> int:
    """把 src 下满足 predicate(relpath) 的文件复制到 out（保留相对路径）。
    返回文件数。"""
    count = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if not predicate(rel.as_posix()):
            continue
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", required=True, help="install 目录（完整包内容）")
    ap.add_argument("--out", required=True, help="拆分输出目录")
    ap.add_argument("--tag", required=True, help="构建 tag")
    ap.add_argument("--arch", required=True, choices=["x86_64", "aarch64"])
    ap.add_argument("--mfa-tag", required=True, help="MFAAvalonia 版本 tag")
    ap.add_argument("--depskey", required=True, help="AgentRT 依赖指纹（短 hash）")
    args = ap.parse_args()

    install = Path(args.install)
    out = Path(args.out)
    if not install.is_dir():
        print(f"install 目录不存在: {install}")
        return 1
    # 干净重建，避免缓存残留混入
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    def top(rel: str) -> str:
        return rel.split("/")[0]

    # ---- Base：GUI + runtimes + tools + docs（除 agent/resource/interface 外全部）----
    n = copy_dir(install, out / "base",
                 lambda rel: top(rel) not in AGENT_PKG_PARTS)
    print(f"[Base] base/: {n} files (MaaGF1-Base-{args.mfa_tag}-{args.arch})")

    # ---- AgentRT：agent/dist + agent/agent.conf（不含 agent/src）----
    def is_rt(rel: str) -> bool:
        if top(rel) != "agent":
            return False
        parts = rel.split("/")
        if parts[1] == "dist":
            return True
        return len(parts) == 2  # agent/xxx（agent.conf 等顶层文件）

    n = copy_dir(install, out / "agent-rt", is_rt)
    print(f"[AgentRT] agent-rt/: {n} files (MaaGF1-AgentRT-{args.depskey}-{args.arch})")

    # ---- Agent：agent/src + resource + resource_en + interface.json ----
    def is_agent(rel: str) -> bool:
        if top(rel) in ("resource", "resource_en", "interface.json"):
            return True
        if top(rel) == "agent":
            parts = rel.split("/")
            return len(parts) >= 3 and parts[1] == "src"
        return False

    n = copy_dir(install, out / "agent", is_agent)
    print(f"[Agent] agent/: {n} files (MaaGF1-Agent-{args.tag}-{args.arch})")

    # ---- 报告尺寸 ----
    for d in sorted(out.iterdir()):
        if d.is_dir():
            total = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            print(f"  {d.name}/: {total / 1024 / 1024:.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
