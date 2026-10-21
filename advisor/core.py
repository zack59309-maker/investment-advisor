# -*- coding: utf-8 -*-
"""基础工具：本地缓存、多源重试调用、数值处理。"""
from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cache")
RUN_DIR = os.path.join(ROOT, "data", "runs")
REPORT_DIR = os.path.join(ROOT, "reports")

for _d in (CACHE_DIR, RUN_DIR, REPORT_DIR):
    os.makedirs(_d, exist_ok=True)


@dataclass
class FetchLog:
    """记录每个数据源的可用性，供报告里的"数据体检"使用。"""

    rows: list = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append({"数据源": name, "状态": status, "说明": detail})

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def _cache_path(name: str) -> str:
    safe = hashlib.md5(name.encode("utf-8")).hexdigest()[:10]
    return os.path.join(CACHE_DIR, f"{name.replace('/', '_')[:60]}_{safe}.pkl")


def cached(name: str, fn: Callable[[], pd.DataFrame], ttl_hours: float = 6.0,
           log: FetchLog | None = None) -> pd.DataFrame | None:
    """带 TTL 的本地缓存；失败返回 None 而不是抛异常。"""
    path = _cache_path(name)
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if age_h < ttl_hours:
            try:
                df = pd.read_pickle(path)
                if log is not None:
                    log.add(name, "缓存", f"{age_h:.1f}h 前，{len(df)} 行")
                return df
            except Exception:  # noqa: BLE001
                pass

    last_err = ""
    for attempt in range(3):
        try:
            df = fn()
            if df is None or (hasattr(df, "empty") and df.empty):
                last_err = "返回空"
                time.sleep(0.8 + attempt)
                continue
            df.to_pickle(path)
            if log is not None:
                log.add(name, "在线", f"{len(df)} 行")
            return df
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            time.sleep(0.8 + attempt)
    if log is not None:
        log.add(name, "失败", last_err)
    # 在线失败时退回过期缓存
    if os.path.exists(path):
        try:
            df = pd.read_pickle(path)
            if log is not None:
                log.add(name, "过期缓存", f"{len(df)} 行（在线抓取失败）")
            return df
        except Exception:  # noqa: BLE001
            pass
    return None


def fetch_first(candidates: list[tuple[str, Callable[[], pd.DataFrame]]],
                ttl_hours: float = 6.0, log: FetchLog | None = None) -> pd.DataFrame | None:
    """多源降级：按顺序尝试候选数据源，返回第一个成功的。"""
    for name, fn in candidates:
        df = cached(name, fn, ttl_hours=ttl_hours, log=log)
        if df is not None and not df.empty:
            return df
    return None


def to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def num(x: Any) -> float:
    v = pd.to_numeric(x, errors="coerce")
    return float(v) if pd.notna(v) else float("nan")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def dump_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def safe(fn: Callable, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return None
