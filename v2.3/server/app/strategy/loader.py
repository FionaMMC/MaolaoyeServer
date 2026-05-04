"""扫描 plugins/ 目录加载策略类。"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Type

from app.strategy.base import Strategy

logger = logging.getLogger(__name__)


def load_plugins(plugins_dir: Path | str) -> dict[str, Type[Strategy]]:
    plugins_dir = Path(plugins_dir)
    if not plugins_dir.exists():
        logger.warning("plugins_dir 不存在: %s", plugins_dir)
        return {}

    registry: dict[str, Type[Strategy]] = {}

    for py_file in sorted(plugins_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        try:
            module = _import_file(py_file)
        except Exception as e:
            logger.error("plugin 加载失败 %s: %s", py_file.name, e)
            continue

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Strategy or not issubclass(obj, Strategy):
                continue
            if obj.__module__ != module.__name__:
                continue
            if not obj.name:
                logger.error("strategy class %s 无 .name 属性", name)
                continue
            if obj.name in registry:
                raise ValueError(
                    f"strategy name 冲突: '{obj.name}' 已被 "
                    f"{registry[obj.name].__module__} 注册，"
                    f"无法再注册 {obj.__module__}"
                )
            registry[obj.name] = obj
            logger.info("注册策略 '%s' from %s", obj.name, py_file.name)

    return registry


def _import_file(path: Path):
    mod_name = f"_qmt_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
