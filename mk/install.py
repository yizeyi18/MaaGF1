# mk/install.py
import shutil
import sys
import json
import argparse
import subprocess
from pathlib import Path

# Import common and configure
sys.path.append(str(Path(__file__).parent))
from common import (
    PROJECT_ROOT, DIST_DIR, DEPS_BIN_DIR, ASSETS_DIR, 
    AGENT_DIR, AGENT_MK_DIR, TOOLS_DIR, DOCS_FILES
)
from configure import configure_ocr_model

def parse_args():
    parser = argparse.ArgumentParser(description="MaaGFL Installer")
    parser.add_argument("version", help="Version tag (e.g., v1.0.0)")
    parser.add_argument("--variant", choices=["standard", "with-agent"], 
                        default="standard", help="Build variant")
    return parser.parse_args()

def clean_install_dir():
    if DIST_DIR.exists():
        print(f"Cleaning previous installation at {DIST_DIR}...")
        shutil.rmtree(DIST_DIR)

def install_deps():
    """
    Copy MFAAvalonia binary files (v1.8+ structure)
    """
    if not DEPS_BIN_DIR.exists() or not any(DEPS_BIN_DIR.iterdir()):
        print(f"Error: Dependencies not found in {DEPS_BIN_DIR}")
        sys.exit(1)

    print(f"Copying binaries from {DEPS_BIN_DIR} to {DIST_DIR}...")
    shutil.copytree(
        DEPS_BIN_DIR,
        DIST_DIR,
        ignore=shutil.ignore_patterns("*.pdb", "*.xml", "MFAUpdater*", "*.zip"),
        dirs_exist_ok=True
    )

def install_resources(version: str, variant: str):
    print(f"Installing resources (Variant: {variant})...")
    
    # 1. Configure OCR
    configure_ocr_model()

    # 2. Copy resource folders
    for folder in ["resource", "resource_en"]:
        src = ASSETS_DIR / folder
        dst = DIST_DIR / folder
        if src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)

    # 3. Handle interface.json
    src_interface = ASSETS_DIR / "interface.json"
    dst_interface = DIST_DIR / "interface.json"
    
    if src_interface.exists():
        shutil.copy2(src_interface, DIST_DIR)
        try:
            with open(dst_interface, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Update Version
            data["version"] = version

            # Inject Agent Config if variant is 'with-agent'
            if variant == "with-agent":
                print("Injecting agent configuration into interface.json...")
                data["agent"] = {
                    "child_exec": "{PROJECT_DIR}/agent/dist/maa_agent.exe",
                    "child_args": []
                }
            
            with open(dst_interface, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            print(f"Updated interface.json (Version: {version}, Agent: {variant})")
        except Exception as e:
            print(f"Warning: Failed to update interface.json: {e}")

def install_agent(variant: str):
    """Install agent based on variant"""
    print(f"Installing agent component (Mode: {variant})...")
    
    dest_agent_dir = DIST_DIR / "agent"
    
    if variant == "standard":
        # Copy source code
        if AGENT_DIR.exists():
            print(f"Copying agent source from {AGENT_DIR}")
            shutil.copytree(
                AGENT_DIR, 
                dest_agent_dir, 
                ignore=shutil.ignore_patterns("__pycache__", "dist", "build", "mk"), 
                dirs_exist_ok=True
            )
    
    elif variant == "with-agent":
        # onedir 布局：PyInstaller 输出到 agent/dist/maa_agent/（exe + _internal/）。
        # 用 onedir 而非 onefile：onefile 冷启动要解压 ~100MB 到临时目录
        # （30-60s），超出 GUI 的 agent 连接重试窗口（~8s）导致"Agent 启动失败"。
        onedir_dir = AGENT_DIR / "dist" / "maa_agent"
        prebuilt_exe = onedir_dir / "maa_agent.exe"

        if prebuilt_exe.exists():
            print(f"Found cached agent build at {prebuilt_exe}. Skipping compilation.")
        else:
            # Compile
            build_script = AGENT_MK_DIR / "build.py"
            if not build_script.exists():
                print(f"Error: Build script not found at {build_script}")
                sys.exit(1)

            print("Executing agent build script...")
            subprocess.check_call([sys.executable, str(build_script)])

        # Copy artifacts：把 dist/maa_agent/* 摊平到 <project>/agent/dist/
        #   → <project>/agent/dist/maa_agent.exe
        #   → <project>/agent/dist/_internal/
        # 与 interface.json 的 child_exec（agent/dist/maa_agent.exe）一致，
        # 且 PyInstaller onedir 的 _internal 与 exe 同目录可被自动发现。
        dest_dist = dest_agent_dir / "dist"

        if onedir_dir.exists():
            print(f"Copying compiled agent (onedir) from {onedir_dir}")
            shutil.copytree(onedir_dir, dest_dist, dirs_exist_ok=True)

            # Also copy agent.conf if needed
            src_conf = AGENT_DIR / "agent.conf"
            if src_conf.exists():
                shutil.copy2(src_conf, dest_agent_dir)
        else:
            print("Error: Compiled artifacts not found after build/cache check.")
            sys.exit(1)

        # 一方源码（打包拆分）：exe 只冻结第三方依赖，
        # main/maa_mcp/sm/utils/action/my_reco 以源码形式分发到
        # <project>/agent/src/，bootstrap 运行时动态导入。
        # 这样 Python 侧改动无需重编译、无需重新下载 exe。
        dest_source = dest_agent_dir / "src"
        print(f"Copying agent source to {dest_source}")
        shutil.copytree(
            AGENT_DIR,
            dest_source,
            ignore=shutil.ignore_patterns("__pycache__", "dist", "build", "mk"),
            dirs_exist_ok=True,
        )

def install_tools():
    if TOOLS_DIR.exists():
        print(f"Installing tools: {TOOLS_DIR}")
        shutil.copytree(TOOLS_DIR, DIST_DIR / "tools", dirs_exist_ok=True)

def install_docs():
    for file_name in DOCS_FILES:
        src = PROJECT_ROOT / file_name
        if src.exists():
            shutil.copy2(src, DIST_DIR)

if __name__ == "__main__":
    args = parse_args()
    ver = args.version
    variant = args.variant
    
    print(f"Starting installation for version: {ver}")
    print(f"Variant: {variant}")
    print(f"Project Root: {PROJECT_ROOT}")
    
    clean_install_dir()
    install_deps()
    install_resources(ver, variant)
    install_agent(variant)
    install_tools()
    install_docs()
    
    print(f"[OK] Installation completed successfully at: {DIST_DIR}")