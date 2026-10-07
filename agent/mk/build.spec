# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

SPEC_DIR = Path(os.getcwd()).resolve()
AGENT_ROOT = SPEC_DIR.parent

if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

block_cipher = None

# MCP 服务器依赖（mcp SDK + uvicorn + starlette 链）——collect_all 收全动态导入
_extra_binaries, _extra_datas, _extra_hidden = [], [], []
for _pkg in ['mcp', 'uvicorn', 'starlette', 'anyio', 'sse_starlette', 'httpx_sse']:
    try:
        from PyInstaller.utils.hooks import collect_all
        _b, _d, _h = collect_all(_pkg)
        _extra_binaries += _b
        _extra_datas += _d
        _extra_hidden += _h
    except Exception as _e:  # 包缺失时跳过（MCP 功能不可用但不阻塞构建）
        print(f'[build.spec] collect_all({_pkg}) skipped: {_e}')

a = Analysis(
    [str(AGENT_ROOT / 'main.py')], 
    pathex=[str(AGENT_ROOT)],
    binaries=_extra_binaries,
    datas=[
        (str(AGENT_ROOT / 'agent.conf'), '.'),
    ] + _extra_datas,
    hiddenimports=[
        'maa', 
        'maa.agent.agent_server',
        'maa.toolkit',
        'my_reco',
        'action',
        'server',
        'config',
        'utils',
        'utils.config',
        'utils.png',
        'sm',
        'sm.core',
        'sm.states_common',
        'sm.states_81n',
        'sm.flows_81n',
        'sm.maa_bridge',
        'sm.sm_action',
        'sm.missing',
        'maa_mcp',
        'maa_mcp.config',
        'maa_mcp.hub',
        'maa_mcp.session',
        'maa_mcp.bridge',
        'maa_mcp.tools',
        'maa_mcp.server',
    ] + _extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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