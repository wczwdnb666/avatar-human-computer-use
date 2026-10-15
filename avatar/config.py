# -*- coding: utf-8 -*-
"""全局配置。零依赖，用 JSON 存用户偏好。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


def _default_config_dir() -> Path:
    """配置目录：%APPDATA%\\Avatar，取不到则退回用户主目录。"""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "Avatar"
    return Path.home() / ".avatar"


@dataclass
class Config:
    # --- DPI ---
    #: 强制要求 DPI 感知初始化成功，失败即抛异常。默认 True ——
    #: 静默的坐标偏移比直接报错危险得多。
    strict_dpi: bool = True

    # --- 感知 ---
    #: 抓屏是否包含分层窗口（半透明 / 部分叠加层）
    capture_layered: bool = True
    #: 截图降采样倍率（1 = 不降采样）。给"看个大概"的场景省带宽。
    capture_downsample: int = 1

    # --- 真人化（阶段四）---
    human_mouse: bool = True
    human_keyboard: bool = True
    motion_speed: float = 1.0  # 1.0 = 标准；>1 更快，<1 更慢

    # --- 风险（阶段五）---
    #: 高危动作是否需要人工确认
    confirm_high_risk: bool = True
    #: 风险闸门总开关。关掉等于放弃安全边界，仅调试用。
    risk_gate_enabled: bool = True

    # --- 日志 ---
    log_level: str = "INFO"
    #: 步骤级 trace 落盘目录（相对工作目录）
    trace_dir: str = "avatar_trace"

    # --- 运行时 ---
    default_timeout: float = 10.0
    poll_interval: float = 0.05

    #: 配置来源标记（"default" / "file" / "env"）
    _source: str = field(default="default", repr=False)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        cfg = cls()
        p = Path(path) if path else _default_config_dir() / "config.json"

        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k) and not k.startswith("_"):
                        setattr(cfg, k, v)
                cfg._source = "file:%s" % p
            except Exception:
                # 配置坏了不该让程序起不来，用默认值并保留来源线索
                cfg._source = "default(坏配置 %s)" % p

        # 环境变量覆盖：AVATAR_<大写字段名>
        for k in list(asdict(cfg).keys()):
            env = os.environ.get("AVATAR_" + k.upper())
            if env is None:
                continue
            cur = getattr(cfg, k)
            try:
                if isinstance(cur, bool):
                    setattr(cfg, k, env.strip().lower() in ("1", "true", "yes", "on"))
                elif isinstance(cur, int):
                    setattr(cfg, k, int(env))
                elif isinstance(cur, float):
                    setattr(cfg, k, float(env))
                else:
                    setattr(cfg, k, env)
                cfg._source += "+env"
            except ValueError:
                pass

        return cfg

    def save(self, path: Optional[Path] = None) -> Path:
        p = Path(path) if path else _default_config_dir() / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["_source"] = self._source
        return d


#: 进程级默认配置。CLI 启动时按需覆盖。
CONFIG = Config.load()
