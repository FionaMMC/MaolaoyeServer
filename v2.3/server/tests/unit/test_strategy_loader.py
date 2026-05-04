"""plugin loader 测试"""
from pathlib import Path

import pytest

from app.strategy.base import Strategy
from app.strategy.loader import load_plugins


def _write(p: Path, content: str):
    p.write_text(content, encoding="utf-8")


def test_load_empty_dir(tmp_path: Path):
    assert load_plugins(tmp_path) == {}


def test_load_nonexistent_dir(tmp_path: Path):
    assert load_plugins(tmp_path / "nope") == {}


def test_load_single_plugin(tmp_path: Path):
    _write(tmp_path / "my_strat.py", '''
from app.strategy.base import Strategy

class MyStrat(Strategy):
    name = "my_strat"
    def run(self, ctx, trade_date):
        return []
''')
    registry = load_plugins(tmp_path)
    assert "my_strat" in registry
    assert issubclass(registry["my_strat"], Strategy)
    assert registry["my_strat"].name == "my_strat"


def test_load_skips_init_py(tmp_path: Path):
    _write(tmp_path / "__init__.py", "# nothing")
    _write(tmp_path / "good.py", '''
from app.strategy.base import Strategy

class S(Strategy):
    name = "good"
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert list(registry.keys()) == ["good"]


def test_load_includes_underscore_prefixed_files(tmp_path: Path):
    _write(tmp_path / "_example.py", '''
from app.strategy.base import Strategy

class Ex(Strategy):
    name = "example"
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert "example" in registry


def test_load_duplicate_name_raises(tmp_path: Path):
    _write(tmp_path / "a.py", '''
from app.strategy.base import Strategy
class A(Strategy):
    name = "dup"
    def run(self, ctx, trade_date): return []
''')
    _write(tmp_path / "b.py", '''
from app.strategy.base import Strategy
class B(Strategy):
    name = "dup"
    def run(self, ctx, trade_date): return []
''')
    with pytest.raises(ValueError, match="name 冲突"):
        load_plugins(tmp_path)


def test_load_plugin_with_import_error_skipped(tmp_path: Path):
    _write(tmp_path / "bad.py", "raise ImportError('boom')")
    _write(tmp_path / "good.py", '''
from app.strategy.base import Strategy
class G(Strategy):
    name = "good"
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert "good" in registry
    assert "bad" not in registry


def test_load_plugin_without_name_skipped(tmp_path: Path):
    _write(tmp_path / "noname.py", '''
from app.strategy.base import Strategy
class NoName(Strategy):
    def run(self, ctx, trade_date): return []
''')
    registry = load_plugins(tmp_path)
    assert registry == {}
