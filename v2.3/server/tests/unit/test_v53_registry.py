"""V53Adapter 必须能被 load_plugins('plugins') auto-discover 并以 name='v53' 注册。

Task 16: 验证 V53Adapter 出现在 strategy registry。无需手动注册——loader 已经
在所有 plugins/*.py 里扫描 Strategy 子类，按 .name 字段挂键。
"""
from __future__ import annotations

from pathlib import Path


def test_v53_adapter_auto_registered():
    from app.strategy.loader import load_plugins
    from plugins.v53_adapter import V53Adapter

    plugins_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
    reg = load_plugins(plugins_dir)

    assert "v53" in reg, f"v53 不在 registry; keys={sorted(reg)}"
    assert reg["v53"].name == "v53"
    # 注：loader 用 importlib 重新加载 module，class identity 不等同 import 的 V53Adapter,
    # 但 .name + .data_files + .data_dir 必须一致
    assert reg["v53"].data_files == V53Adapter.data_files
    assert reg["v53"].data_dir == V53Adapter.data_dir


def test_v53_does_not_conflict_with_v20h():
    """v53 + v20h_v1_3 应能同时注册，不冲突"""
    from app.strategy.loader import load_plugins
    plugins_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
    reg = load_plugins(plugins_dir)
    assert "v53" in reg
    assert "v20h_v1_3" in reg
    assert reg["v53"] is not reg["v20h_v1_3"]
