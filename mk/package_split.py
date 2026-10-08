# mk/package_split.py
"""把 install/ 目录拆成三个分发件（打包拆分）。

背景：agent 可执行文件只冻结第三方依赖（bootstrap 设计），
一方 Python 代码以源码形式分发。整包 100+ MiB 里大部分
（GUI 运行时 / PyInstaller 产物）极少变化，不应每次构建
都让用户重新下载。

产出（zip 输出到 --out 目录）：
  MaaGF1-Base-<mfa_tag>-<arch>.zip     GUI + runtimes + tools + docs
                                       （MFAAvalonia 版本变化才需要重新下载）
  MaaGF1-AgentRT-<depskey>-<arch>.zip  agent/dist（exe + conf）
                                       （第三方依赖变化才需要重新下载）
  MaaGF1-Agent-<tag>-<arch>.zip        agent/src + resource + interface.json
                                       （每次构建；Python 侧改动的最小下载单元）

用法：
  python mk/package_split.py --install install --out split \
      --tag v1.2.3 --arch x86_64 --mfa-tag v2.10.8 --depskey abc12345
"""
import argparse
import shutil
import sys
import zipfile
from pathlib import Path

# 属于"每次构建"件的内容
AGENT_PKG_PARTS = ("agent", "resource", "resource_en", "interface.json")
# 属于 AgentRT 的内容
AGENT_RT_PARTS = ("agent",)


def zip_dir(src: Path, out: Path, predicate) -> int:
    """把 src 下满足 predicate(relpath) 的文件打进 out（zip）。返回文件数。"""
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src).as_posix()
            if not predicate(rel):
                continue
            zf.write(p, rel)
            count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", required=True, help="install 目录（完整包内容）")
    ap.add_argument("--out", required=True, help="zip 输出目录")
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
    out.mkdir(parents=True, exist_ok=True)

    def top(rel: str) -> str:
        return rel.split("/")[0]

    # ---- Base：GUI + runtimes + tools + docs（除 agent/resource/interface 外全部）----
    base_name = f"MaaGF1-Base-{args.mfa_tag}-{args.arch}.zip"
    n = zip_dir(install, out / base_name,
                lambda rel: top(rel) not in AGENT_PKG_PARTS)
    print(f"[Base] {base_name}: {n} files")

    # ---- AgentRT：agent/dist + agent/agent.conf（不含 agent/src）----
    rt_name = f"MaaGF1-AgentRT-{args.depskey}-{args.arch}.zip"

    def is_rt(rel: str) -> bool:
        if top(rel) != "agent":
            return False
        parts = rel.split("/")
        if parts[1] == "dist":
            return True
        return len(parts) == 2  # agent/xxx（agent.conf 等顶层文件）

    n = zip_dir(install, out / rt_name, is_rt)
    print(f"[AgentRT] {rt_name}: {n} files")

    # ---- Agent：agent/src + resource + resource_en + interface.json ----
    ag_name = f"MaaGF1-Agent-{args.tag}-{args.arch}.zip"

    def is_agent(rel: str) -> bool:
        if top(rel) in ("resource", "resource_en", "interface.json"):
            return True
        if top(rel) == "agent":
            parts = rel.split("/")
            return len(parts) >= 3 and parts[1] == "src"
        return False

    n = zip_dir(install, out / ag_name, is_agent)
    print(f"[Agent] {ag_name}: {n} files")

    # ---- 报告尺寸 ----
    for z in sorted(out.glob("*.zip")):
        print(f"  {z.name}: {z.stat().st_size / 1024 / 1024:.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
