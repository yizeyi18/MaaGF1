# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

SPEC_DIR = Path(os.getcwd()).resolve()
AGENT_ROOT = SPEC_DIR.parent

if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

block_cipher = None

# MCP 服务器依赖（mcp SDK + uvicorn + starlette 完整依赖闭包）——collect_all 收全动态导入
# 注意：mcp 2.x 的 import 全部是运行时动态导入（函数内 import），
# PyInstaller 静态分析看不到，必须在这里显式收集。
# 列表来源：在 venv 里实际 import mcp.server.mcpserver 并构建 app 后
# 追踪 sys.modules 得到的完整第三方包集合。
_extra_binaries, _extra_datas, _extra_hidden = [], [], []
_mcp_pkgs = [
    'mcp', 'mcp_types',            # SDK 本体 + 类型包（PyPI: mcp-types）
    'uvicorn', 'h11',              # HTTP 服务器 + h11 协议实现
    'starlette', 'sse_starlette', 'anyio', 'httpx_sse',
    'httpx2',                      # mcp 2.x 用的 httpx 分支
    'pydantic', 'pydantic_core', 'annotated_types', 'typing_inspection',
    'cryptography', 'idna', 'python_multipart', 'click',
    'opentelemetry',               # mcp.shared._otel 无条件导入（opentelemetry-api）
]
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs


def _collect(pkg):
    """collect_all 优先；失败时退化为 walk_packages（不 import 子包）。

    mcp 的 collect_all 会失败：PyInstaller 收集子模块时 import mcp.cli，
    而 mcp.cli 依赖可选的 typer（未安装）→ 子进程退出。
    退化路径用 pkgutil.walk_packages 只读目录列表，不触发 import。
    """
    try:
        return collect_all(pkg)
    except Exception as e:
        print(f'[build.spec] collect_all({pkg}) failed ({type(e).__name__}), fallback')
    import importlib
    import importlib.util
    import pkgutil

    # 注意：pkgutil.walk_packages 会 import 子包（mcp.cli 缺 typer 会 sys.exit），
    # 所以用 find_spec 手动递归（只读 spec，不执行模块代码）。
    mod = importlib.import_module(pkg)
    hidden = [pkg]

    def _walk(paths, prefix):
        for info in pkgutil.iter_modules(paths, prefix):
            # 精确跳过 mcp.cli（依赖可选 typer，其模块级 sys.exit 会干扰分析）
            if info.name == pkg + '.cli' or info.name.startswith(pkg + '.cli.'):
                continue
            hidden.append(info.name)
            if info.ispkg:
                spec = importlib.util.find_spec(info.name)
                if spec is not None and spec.submodule_search_locations:
                    _walk(list(spec.submodule_search_locations), info.name + '.')

    _walk(list(getattr(mod, '__path__', []) or []), pkg + '.')
    try:
        datas = collect_data_files(pkg)
    except Exception:
        datas = []
    try:
        bins = collect_dynamic_libs(pkg)
    except Exception:
        bins = []
    return bins, datas, hidden


for _pkg in _mcp_pkgs:
    _b, _d, _h = _collect(_pkg)
    _extra_binaries += _b
    _extra_datas += _d
    _extra_hidden += _h
    print(f'[build.spec] collected {_pkg}: binaries={len(_b)} datas={len(_d)} hidden={len(_h)}')

a = Analysis(
    [str(AGENT_ROOT / 'bootstrap.py')],
    pathex=[str(AGENT_ROOT)],
    binaries=_extra_binaries,
    datas=[
        (str(AGENT_ROOT / 'agent.conf'), '.'),
    ] + _extra_datas,
    # 只冻结第三方依赖：maa 全家桶 + MCP 服务器栈（_extra_hidden）+ numpy/requests。
    # 一方代码（main/my_reco/action/utils/sm/maa_mcp）不进 PYZ——
    # 以源码形式分发在 <project>/agent/src/，bootstrap 运行时动态导入。
    hiddenimports=[
        'maa',
        'maa.agent.agent_server',
        'maa.toolkit',
        'maa.controller',
        'maa.resource',
        'maa.tasker',
        'maa.pipeline',
        'maa.context',
        'maa.job',
        'maa.buffer',
        'maa.define',
        'maa.event_sink',
        'maa.custom_action',
        'maa.library',
        'numpy',
        'requests',
    ] + _extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 防止 PyInstaller 从 pathex 把一方源码误打进 PYZ
    excludes=[
        'main', 'my_reco', 'action', 'utils', 'sm', 'maa_mcp',
        'server', 'config',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyd = []
for d in a.datas:
    if 'pyconfig' not in d[0]:
        pyd.append(d)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='maa_agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)