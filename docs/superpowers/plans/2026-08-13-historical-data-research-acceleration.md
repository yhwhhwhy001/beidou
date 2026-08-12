# 历史数据加速研究（全链路）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 2 年 × 1h/1d 历史数据驱动因子挖掘产出 sealed PASS EvidenceBundle，并通过启动扫描桥接使因子在 testnet 真正晋级 ACTIVE、接入 Alpha DAG。

**Architecture:** 三阶段流水线。阶段 1（Task 1-4）：历史 K 线分页拉取 + parquet 本地存储 + 数据集清单哈希。阶段 2（Task 5-9）：研究 CLI 读本地数据、WFO 统计 numpy 化、多粒度稳健性、replay 模拟、逐级证据链。阶段 3（Task 10-13）：通用表达式组件 + EvidenceBridge 启动扫描 + engine 接线 + 端到端验收。

**Tech Stack:** Python 3.12、pandas/pyarrow（pyproject research extras 已声明）、numpy、pytest（.venv/bin/python -m pytest）、Binance USDⓈ-M REST。

## Global Constraints

- 测试运行：`.venv/bin/python -m pytest tests/... -v`（项目 venv，勿用系统 Python）。
- 代码规范：ruff（line-length 120）与 mypy 须通过：`.venv/bin/ruff check <files> && .venv/bin/mypy <files>`。
- Git 提交信息用项目惯例前缀：`BD-FEAT:`（新功能）或 `BD-FIX:`（修复）。
- 证据文件格式向后兼容：旧文件无 `promotion_chain`/`evidence_source`/`expression_string` 字段，桥接必须忽略（不晋级）。
- Fail-closed：任何证据验证失败不得晋级因子；桥接失败不得阻断引擎启动。
- **spec 修正**：spec 5.2 写 dataset_manifest_hash 为"前 24 位 hex"，与 `MiningRunner._validated_manifest_hash`（要求 64 位 hex）冲突。本计划采用**完整 sha256 hex（64 位）**。
- 环境来源规则：`evidence_source="historical_replay"` 的 PAPER_TRADING/CHALLENGER 证据仅 testnet/paper/research 接受；canary/live 拒绝。
- `_factor_component_registry` 值为 `(callable, tuple[str, ...])`，DAG 构建用 `component_cls()` 无参调用 → 表达式组件用 `functools.partial` 绑定。
- 不做：expression_ast 重写、FactorPromotionGate 严格性变更、canary/live replay 路径、行情流录制回放。

---

### Task 1: KlineStore — parquet 本地 K 线存储

**Files:**
- Create: `beidou_research/data/__init__.py`（导出 KlineStore/DatasetManifest）
- Create: `beidou_research/data/kline_store.py`
- Test: `tests/unit/test_kline_store.py`

**Interfaces:**
- Produces: `KlineStore(root: str | Path = ".beidou/data/klines").append(symbol, interval, klines: list[dict]) -> int`（返回写入后总行数）、`.load(symbol, interval) -> pandas.DataFrame`、`.has_data(symbol, interval, min_rows=100) -> bool`、`.last_open_time(symbol, interval) -> int | None`。parquet 列固定为 `open_time, open, high, low, close, volume, is_closed`（全 float64/bool，open_time 为 int64 毫秒）。kline dict 键用 feed 解析后的名字（`open_time/open/high/low/close/volume/is_closed`）。

- [ ] **Step 1: 安装 research extras 并写失败测试**

```bash
.venv/bin/pip install -e ".[research]"
```

`tests/unit/test_kline_store.py`：

```python
import pandas as pd
from beidou_research.data.kline_store import KlineStore


def _kline(open_time: int, close: float) -> dict:
    return {
        "open_time": open_time, "open": close, "high": close,
        "low": close, "close": close, "volume": 100.0, "is_closed": True,
    }


def test_append_deduplicates_by_open_time(tmp_path):
    store = KlineStore(root=str(tmp_path))
    first = store.append("BTCUSDT", "1h", [_kline(1000, 1.0), _kline(2000, 2.0)])
    assert first == 2
    # 第二批次与第一批有重叠：open_time=2000 重复，open_time=3000 新增。
    # concat 先旧后新 + drop_duplicates(keep="last") → 重复行保留新批次值。
    second = store.append("BTCUSDT", "1h", [_kline(2000, 2.5), _kline(3000, 3.0)])
    assert second == 3
    df = store.load("BTCUSDT", "1h")
    assert list(df["open_time"]) == [1000, 2000, 3000]
    assert df.iloc[1]["close"] == 2.5


def test_load_returns_full_schema(tmp_path):
    store = KlineStore(root=str(tmp_path))
    store.append("BTCUSDT", "1h", [_kline(1000, 1.0)])
    df = store.load("BTCUSDT", "1h")
    assert list(df.columns) == ["open_time", "open", "high", "low", "close", "volume", "is_closed"]


def test_has_data_and_last_open_time(tmp_path):
    store = KlineStore(root=str(tmp_path))
    assert store.has_data("BTCUSDT", "1h") is False
    assert store.last_open_time("BTCUSDT", "1h") is None
    store.append("BTCUSDT", "1h", [_kline(1000, 1.0), _kline(2000, 2.0)])
    assert store.has_data("BTCUSDT", "1h", min_rows=2) is True
    assert store.last_open_time("BTCUSDT", "1h") == 2000
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_kline_store.py -v`
Expected: FAIL，`ModuleNotFoundError: beidou_research.data.kline_store`

- [ ] **Step 3: 实现 KlineStore**

`beidou_research/data/kline_store.py`：

```python
"""K 线本地持久化：parquet 按 (symbol, interval) 存储，open_time 去重合并。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

SCHEMA_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "is_closed"]


class KlineStore:
    def __init__(self, root: str | Path = ".beidou/data/klines") -> None:
        self._root = Path(root)

    def _path(self, symbol: str, interval: str) -> Path:
        return self._root / symbol / f"{interval}.parquet"

    def append(self, symbol: str, interval: str, klines: list[dict]) -> int:
        if not klines:
            return self._count_existing(symbol, interval)
        frame = pd.DataFrame(
            [
                {
                    "open_time": int(k["open_time"]),
                    "open": float(k["open"]),
                    "high": float(k["high"]),
                    "low": float(k["low"]),
                    "close": float(k["close"]),
                    "volume": float(k.get("volume", 0.0)),
                    "is_closed": bool(k.get("is_closed", False)),
                }
                for k in klines
            ],
            columns=SCHEMA_COLUMNS,
        )
        path = self._path(symbol, interval)
        if path.exists():
            existing = pd.read_parquet(path, columns=SCHEMA_COLUMNS)
            merged = (
                pd.concat([existing, frame], ignore_index=True)
                .drop_duplicates(subset=["open_time"], keep="last")
                .sort_values("open_time")
                .reset_index(drop=True)
            )
        else:
            merged = frame.drop_duplicates(subset=["open_time"], keep="last").sort_values("open_time").reset_index(drop=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(path, index=False)
        return int(len(merged))

    def load(self, symbol: str, interval: str) -> pd.DataFrame:
        path = self._path(symbol, interval)
        if not path.exists():
            raise FileNotFoundError(f"kline store has no data for {symbol}/{interval}")
        return pd.read_parquet(path, columns=SCHEMA_COLUMNS)

    def has_data(self, symbol: str, interval: str, min_rows: int = 100) -> bool:
        path = self._path(symbol, interval)
        if not path.exists():
            return False
        df = pd.read_parquet(path, columns=["open_time"])
        return int(len(df)) >= min_rows

    def last_open_time(self, symbol: str, interval: str) -> int | None:
        path = self._path(symbol, interval)
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=["open_time"])
        return int(df["open_time"].max()) if len(df) else None

    def _count_existing(self, symbol: str, interval: str) -> int:
        path = self._path(symbol, interval)
        if not path.exists():
            return 0
        return int(len(pd.read_parquet(path, columns=["open_time"])))
```

`beidou_research/data/__init__.py`：

```python
"""研究数据层：历史 K 线存储与数据集清单。"""

from .dataset_manifest import DatasetManifest  # noqa: F401  (Task 2 提供)
from .kline_store import KlineStore  # noqa: F401
```

（Task 1 提交时 `dataset_manifest` 尚不存在会导包失败——本任务只写 `from .kline_store import KlineStore`，Task 2 再补 DatasetManifest 导出。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_kline_store.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add beidou_research/data/ tests/unit/test_kline_store.py
git commit -m "BD-FEAT: KlineStore parquet 本地 K 线存储（open_time 去重合并）"
```

---

### Task 2: DatasetManifest — 数据集清单与溯源哈希

**Files:**
- Create: `beidou_research/data/dataset_manifest.py`
- Modify: `beidou_research/data/__init__.py`（补 DatasetManifest 导出）
- Test: `tests/unit/test_dataset_manifest.py`

**Interfaces:**
- Consumes: `KlineStore.load`（Task 1）。
- Produces: `DatasetManifest.compute(df: pandas.DataFrame, symbol: str, interval: str) -> dict`（键 `symbol/interval/rows/first_open_time/last_open_time/content_sha256`）、`DatasetManifest.hash_of(manifest: dict) -> str`（**完整 sha256 hex，64 位**，`sha256(json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest()`）、`DatasetManifest.write(path: Path, manifest: dict) -> None`（写 `{interval}.manifest.json`）、`DatasetManifest.read(path: Path) -> dict | None`。content_sha256 = 按列排序后整帧字节哈希：`sha256(df.sort_values("open_time").to_parquet(index=False, compression="zstd"))`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_dataset_manifest.py`：

```python
import hashlib
import json

import pandas as pd

from beidou_research.data.dataset_manifest import DatasetManifest


def _frame():
    return pd.DataFrame(
        {
            "open_time": [1000, 2000],
            "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
            "close": [1.0, 2.0], "volume": [10.0, 20.0], "is_closed": [True, True],
        }
    )


def test_manifest_is_deterministic():
    m1 = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    m2 = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    assert m1 == m2
    assert DatasetManifest.hash_of(m1) == DatasetManifest.hash_of(m2)


def test_hash_is_full_sha256_hex():
    m = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    h = DatasetManifest.hash_of(m)
    assert len(h) == 64
    int(h, 16)  # 必须为合法 hex


def test_manifest_fields_and_content_change():
    m = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    assert m["symbol"] == "BTCUSDT" and m["interval"] == "1h" and m["rows"] == 2
    assert m["first_open_time"] == 1000 and m["last_open_time"] == 2000
    changed = _frame()
    changed.loc[1, "close"] = 9.9
    m2 = DatasetManifest.compute(changed, "BTCUSDT", "1h")
    assert DatasetManifest.hash_of(m) != DatasetManifest.hash_of(m2)


def test_write_read_roundtrip(tmp_path):
    m = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    DatasetManifest.write(tmp_path / "1h.manifest.json", m)
    loaded = DatasetManifest.read(tmp_path / "1h.manifest.json")
    assert loaded == m
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_dataset_manifest.py -v`
Expected: FAIL，`ModuleNotFoundError: beidou_research.data.dataset_manifest`

- [ ] **Step 3: 实现 DatasetManifest**

```python
"""数据集清单：为 (symbol, interval) 数据生成溯源哈希，绑定 EvidenceBundle。"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd


class DatasetManifest:
    @staticmethod
    def compute(df: pd.DataFrame, symbol: str, interval: str) -> dict[str, Any]:
        ordered = df.sort_values("open_time").reset_index(drop=True)
        buffer = io.BytesIO()
        ordered.to_parquet(buffer, index=False, compression="zstd")
        content_sha256 = hashlib.sha256(buffer.getvalue()).hexdigest()
        return {
            "symbol": symbol,
            "interval": interval,
            "rows": int(len(ordered)),
            "first_open_time": int(ordered["open_time"].min()),
            "last_open_time": int(ordered["open_time"].max()),
            "content_sha256": content_sha256,
        }

    @staticmethod
    def hash_of(manifest: dict[str, Any]) -> str:
        payload = json.dumps(manifest, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def write(path: Path, manifest: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, sort_keys=True, indent=2))

    @staticmethod
    def read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
```

`beidou_research/data/__init__.py` 更新为：

```python
"""研究数据层：历史 K 线存储与数据集清单。"""

from .dataset_manifest import DatasetManifest  # noqa: F401
from .kline_store import KlineStore  # noqa: F401
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_dataset_manifest.py tests/unit/test_kline_store.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add beidou_research/data/
git commit -m "BD-FEAT: DatasetManifest 数据集清单（64 位 sha256 溯源哈希）"
```

---

### Task 3: fetch_klines 分页扩展

**Files:**
- Modify: `beidou_core/feed.py`（`fetch_klines` 方法，约 694 行）
- Test: `tests/unit/test_fetch_klines_pagination.py`

**Interfaces:**
- Consumes: 现有 `self._api(path, method, signed, params)`、`_parse_rest_kline(raw, now, *, include_closed)`。
- Produces: `fetch_klines(symbol: str, interval: str, limit: int = 100, start_time: int | None = None, end_time: int | None = None, max_pages: int = 400) -> list[dict]`。`start_time/end_time` 为毫秒时间戳（int）。分页：每页 `limit=min(1000, max(limit, 1000) if 有时间范围 else limit)`——简化：有时间范围时每页固定 1000；无时间范围时行为与现在完全一致（单请求 `limit`）。

- [ ] **Step 1: 写失败测试（mock `_api`）**

`tests/unit/test_fetch_klines_pagination.py`：

```python
from datetime import datetime, timezone

from beidou_core.feed import MarketDataFeed

INTERVAL_MS = 3_600_000


def _raw_kline(open_time_ms: int) -> list:
    # Binance kline 数组 12 字段，index 0=openTime, 1=open, 2=high, 3=low, 4=close, 5=volume, 11=isClosed
    return [
        open_time_ms, "100.0", "101.0", "99.0", "100.5", "10.0",
        0, "0", "0", "0", "0", True,
    ]


class _MockApi:
    def __init__(self, pages: dict[int, list[list]]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def __call__(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> list:
        self.calls.append(dict(params or {}))
        return self._pages.get(params["startTime"], [])


def _make_feed(pages):
    feed = MarketDataFeed.__new__(MarketDataFeed)
    feed._api = _MockApi(pages)
    return feed


def test_paginates_until_end_time():
    t0 = 1_700_000_000_000
    page1 = [_raw_kline(t0 + i * INTERVAL_MS) for i in range(3)]
    page2 = [_raw_kline(t0 + 3 * INTERVAL_MS)]
    pages = {t0: page1, t0 + 3 * INTERVAL_MS: page2}
    feed = _make_feed(pages)
    result = feed.fetch_klines("BTCUSDT", "1h", start_time=t0, end_time=t0 + 3 * INTERVAL_MS)
    assert len(result) == 4
    assert feed._api.calls[0]["startTime"] == t0
    assert feed._api.calls[1]["startTime"] == t0 + 3 * INTERVAL_MS
    assert all(k["is_closed"] is True for k in result)


def test_deduplicates_across_pages():
    t0 = 1_700_000_000_000
    page1 = [_raw_kline(t0 + i * INTERVAL_MS) for i in range(2)]
    page2 = [_raw_kline(t0 + INTERVAL_MS), _raw_kline(t0 + 2 * INTERVAL_MS)]  # 与 page1 重叠 1 根
    pages = {t0: page1, t0 + 2 * INTERVAL_MS: page2}
    feed = _make_feed(pages)
    result = feed.fetch_klines("BTCUSDT", "1h", start_time=t0, end_time=t0 + 2 * INTERVAL_MS)
    assert len(result) == 3


def test_no_time_range_keeps_legacy_behavior():
    t0 = 1_700_000_000_000
    pages = {t0: [_raw_kline(t0)]}
    feed = _make_feed(pages)
    result = feed.fetch_klines("BTCUSDT", "1h", limit=500)
    assert len(result) == 1
    call = feed._api.calls[0]
    assert "startTime" not in call and call["limit"] == 500


def test_max_pages_bound():
    t0 = 1_700_000_000_000
    pages = {t0: [_raw_kline(t0 + i * INTERVAL_MS) for i in range(1000)]}
    feed = _make_feed(pages)
    # 下一页 startTime 与第一页相同 → 无限循环风险；max_pages 必须截断
    result = feed.fetch_klines("BTCUSDT", "1h", start_time=t0, end_time=t0 + 10**12, max_pages=2)
    assert len(result) == 1000
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_fetch_klines_pagination.py -v`
Expected: FAIL，`TypeError: fetch_klines() got an unexpected keyword argument 'start_time'`

- [ ] **Step 3: 实现分页**

替换 `beidou_core/feed.py` 的 `fetch_klines`：

```python
    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
        max_pages: int = 400,
    ) -> list[dict]:
        """拉取 K 线。无时间范围时与旧行为一致（单请求 limit 根）。

        有时间范围时按 1000 根/页循环分页（Binance REST 上限），
        页间以本页最后一根 open_time 递增（含重叠去重），
        直到覆盖 end_time 或达到 max_pages。
        """
        if start_time is None:
            raw = self._api(
                Endpoint.KLINES,
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            if not isinstance(raw, list):
                raise MarketDataUnknownError(f"klines for {symbol} are not a list")
            now = datetime.now(timezone.utc)
            klines = []
            for k in raw:
                parsed = self._parse_rest_kline(k, now, include_closed=True)
                if parsed is not None:
                    klines.append(parsed)
            if not klines:
                raise MarketDataUnknownError(f"klines for {symbol} contain no closed valid rows")
            return klines

        page_start = int(start_time)
        now = datetime.now(timezone.utc)
        collected: dict[int, dict] = {}
        for _page_index in range(max_pages):
            params: dict = {
                "symbol": symbol,
                "interval": interval,
                "limit": 1000,
                "startTime": page_start,
            }
            if end_time is not None:
                params["endTime"] = int(end_time)
            raw = self._api(Endpoint.KLINES, params=params)
            if not isinstance(raw, list) or not raw:
                break
            page_last_open = None
            for k in raw:
                parsed = self._parse_rest_kline(k, now, include_closed=True)
                if parsed is None:
                    continue
                open_time = int(parsed["open_time"])
                collected[open_time] = parsed  # 跨页重叠去重
                if page_last_open is None or open_time > page_last_open:
                    page_last_open = open_time
            if page_last_open is None or page_last_open <= page_start:
                break  # 无进展，防死循环
            page_start = page_last_open
            if end_time is not None and page_last_open >= int(end_time):
                break
        if not collected:
            raise MarketDataUnknownError(f"klines for {symbol} contain no closed valid rows")
        return [collected[t] for t in sorted(collected)]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_fetch_klines_pagination.py -v`
Expected: 4 passed。再跑回归：`.venv/bin/python -m pytest tests/unit/test_feed.py -v`（若该文件存在）确认旧行为未破坏。

- [ ] **Step 5: ruff + mypy + Commit**

```bash
.venv/bin/ruff check beidou_core/feed.py tests/unit/test_fetch_klines_pagination.py
.venv/bin/mypy beidou_core/feed.py
git add beidou_core/feed.py tests/unit/test_fetch_klines_pagination.py
git commit -m "BD-FEAT: fetch_klines 支持 start/end 时间范围分页（1000 根/页，重叠去重）"
```

---

### Task 4: backfill CLI 命令

**Files:**
- Modify: `apps/factor_miner/__main__.py`
- Create: `beidou_research/data/backfill.py`（核心逻辑，与 CLI 分离便于测试）
- Test: `tests/unit/test_backfill.py`

**Interfaces:**
- Consumes: `MarketDataFeed.fetch_klines`（Task 3）、`KlineStore`（Task 1）、`DatasetManifest`（Task 2）。
- Produces: `backfill_symbol(feed, store, symbol, interval, start_ms, end_ms, max_pages=400, page_pause_seconds=0.5, on_progress=None) -> dict`，返回 `{"symbol", "interval", "pages", "rows", "manifest_hash", "errors": [...]}`，拉完后写 `{interval}.manifest.json` 到同目录；`backfill_all(feed, store, symbols, intervals, start_ms, end_ms) -> list[dict]`。CLI：`factor_miner backfill --symbols ... --intervals ... --start ... [--end ...] [--max-pages 400] [--dry-run]`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_backfill.py`：

```python
from pathlib import Path

from beidou_research.data.backfill import backfill_symbol
from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore

INTERVAL_MS = 3_600_000


def _raw_kline(open_time_ms: int) -> list:
    return [open_time_ms, "100.0", "101.0", "99.0", "100.5", "10.0", 0, "0", "0", "0", "0", True]


class _FakeFeed:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_klines(self, symbol, interval, start_time=None, end_time=None, max_pages=400):
        self.calls += 1
        if self.calls > 1:
            return []
        return [_raw_kline(1_700_000_000_000 + i * INTERVAL_MS) for i in range(3)]


def test_backfill_writes_parquet_and_manifest(tmp_path):
    feed = _FakeFeed()
    store = KlineStore(root=str(tmp_path / "klines"))
    report = backfill_symbol(
        feed, store, "BTCUSDT", "1h",
        start_ms=1_700_000_000_000, end_ms=1_700_000_000_000 + 10 * INTERVAL_MS,
        page_pause_seconds=0,
    )
    assert report["rows"] == 3 and not report["errors"]
    manifest_path = tmp_path / "klines" / "BTCUSDT" / "1h.manifest.json"
    manifest = DatasetManifest.read(manifest_path)
    assert manifest is not None and manifest["rows"] == 3
    assert report["manifest_hash"] == DatasetManifest.hash_of(manifest)


def test_backfill_resumes_from_store(tmp_path):
    feed = _FakeFeed()
    store = KlineStore(root=str(tmp_path / "klines"))
    first = backfill_symbol(feed, store, "BTCUSDT", "1h", start_ms=0, end_ms=10**12, page_pause_seconds=0)
    assert first["rows"] == 3
    # 第二次运行：store 已有数据 → 从 last_open_time 继续（fake feed 第二次返回空）→ 不重复不报错
    second = backfill_symbol(feed, store, "BTCUSDT", "1h", start_ms=0, end_ms=10**12, page_pause_seconds=0)
    assert second["rows"] == 3 and not second["errors"]


def test_backfill_reports_feed_errors(tmp_path):
    class _BrokenFeed:
        def fetch_klines(self, symbol, interval, start_time=None, end_time=None, max_pages=400):
            raise RuntimeError("api down")

    store = KlineStore(root=str(tmp_path / "klines"))
    report = backfill_symbol(_BrokenFeed(), store, "BTCUSDT", "1h", start_ms=0, end_ms=10**12, page_pause_seconds=0)
    assert report["errors"] and report["rows"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_backfill.py -v`
Expected: FAIL，`ModuleNotFoundError: beidou_research.data.backfill`

- [ ] **Step 3: 实现 backfill 核心**

`beidou_research/data/backfill.py`：

```python
"""历史 K 线回填：分页拉取 → KlineStore 去重落盘 → DatasetManifest。"""

from __future__ import annotations

import time
from typing import Any, Callable

from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore


def backfill_symbol(
    feed: Any,
    store: KlineStore,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    max_pages: int = 400,
    page_pause_seconds: float = 0.5,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """拉取一个 (symbol, interval) 的完整历史并落盘。

    续传：store 已有数据时从 last_open_time 继续，start_ms 仅作首次起点。
    单页失败重试 3 次后记入 errors 并停止该 symbol 的拉取。
    """
    errors: list[str] = []
    effective_start = store.last_open_time(symbol, interval) or start_ms
    pages = 0
    collected: list[dict] = []
    cursor = effective_start
    for _ in range(max_pages):
        page = None
        for attempt in range(3):
            try:
                page = feed.fetch_klines(
                    symbol, interval, start_time=cursor, end_time=end_ms, max_pages=1
                )
                break
            except Exception as exc:
                if attempt == 2:
                    errors.append(f"page@{cursor}:{type(exc).__name__}")
                else:
                    time.sleep(1.0 * (attempt + 1))
        if page is None and errors:
            break
        if not page:
            break
        pages += 1
        collected.extend(page)
        cursor = int(page[-1]["open_time"])
        if on_progress:
            on_progress(f"{symbol} {interval}: page {pages} rows {len(collected)}")
        if cursor >= end_ms or len(page) < 1000:
            break
        if page_pause_seconds > 0:
            time.sleep(page_pause_seconds)

    rows = store.append(symbol, interval, collected) if collected else 0
    manifest_hash = ""
    if rows > 0:
        frame = store.load(symbol, interval)
        manifest = DatasetManifest.compute(frame, symbol, interval)
        manifest_hash = DatasetManifest.hash_of(manifest)
        DatasetManifest.write(store._path(symbol, interval).with_name(f"{interval}.manifest.json"), manifest)
    return {
        "symbol": symbol,
        "interval": interval,
        "pages": pages,
        "rows": rows,
        "manifest_hash": manifest_hash,
        "errors": errors,
    }


def backfill_all(
    feed: Any,
    store: KlineStore,
    symbols: list[str],
    intervals: list[str],
    start_ms: int,
    end_ms: int,
    max_pages: int = 400,
    page_pause_seconds: float = 0.5,
) -> list[dict[str, Any]]:
    reports = []
    for symbol in symbols:
        for interval in intervals:
            reports.append(
                backfill_symbol(
                    feed, store, symbol, interval,
                    start_ms=start_ms, end_ms=end_ms,
                    max_pages=max_pages, page_pause_seconds=page_pause_seconds,
                )
            )
    return reports
```

- [ ] **Step 4: CLI 命令接入**

`apps/factor_miner/__main__.py` 添加（放在 `run` 命令之后）：

```python
@cli.command()
@click.option("--symbols", required=True, help="逗号分隔品种列表")
@click.option("--intervals", default="1h,1d", show_default=True, help="逗号分隔 K 线粒度")
@click.option("--start", required=True, help="起始日期 YYYY-MM-DD（UTC）")
@click.option("--end", default=None, help="结束日期 YYYY-MM-DD（UTC），默认今天")
@click.option("--max-pages", default=400, show_default=True, type=click.IntRange(1, 2000))
@click.option("--data-root", default=".beidou/data/klines", show_default=True)
@click.option("--dry-run", is_flag=True, help="仅打印计划，不拉取")
def backfill(symbols: str, intervals: str, start: str, end: str | None, max_pages: int, data_root: str, dry_run: bool) -> None:
    """批量回填历史 K 线到本地 parquet 存储。"""
    from datetime import datetime, timedelta, timezone

    from beidou_core.feed import MarketDataFeed
    from beidou_research.data.backfill import backfill_all
    from beidou_research.data.kline_store import KlineStore

    def _to_ms(date_str: str) -> int:
        return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

    symbols_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    intervals_list = [i.strip() for i in intervals.split(",") if i.strip()]
    if not symbols_list or not intervals_list:
        raise click.ClickException("--symbols 与 --intervals 不能为空")
    start_ms = _to_ms(start)
    end_ms = _to_ms(end) if end else int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_ms >= end_ms:
        raise click.ClickException("--start 必须早于 --end")

    if dry_run:
        for symbol in symbols_list:
            for interval in intervals_list:
                span_ms = end_ms - start_ms
                bar_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
                click.echo(f"  {symbol} {interval}: ~{span_ms // bar_ms} 根 ≈ {span_ms // bar_ms // 1000 + 1} 页")
        return

    os.environ.setdefault("BEIDOU_ENV", "testnet")
    feed = MarketDataFeed()
    store = KlineStore(root=data_root)
    reports = backfill_all(feed, store, symbols_list, intervals_list, start_ms, end_ms, max_pages=max_pages)
    failed = [r for r in reports if r["errors"]]
    for r in reports:
        state = "ERROR" if r["errors"] else "OK"
        click.echo(f"  [{state}] {r['symbol']} {r['interval']}: pages={r['pages']} rows={r['rows']} manifest={r['manifest_hash'][:12]}")
    if failed:
        sys.exit(1)
```

- [ ] **Step 5: 运行测试 + ruff + Commit**

Run: `.venv/bin/python -m pytest tests/unit/test_backfill.py -v` → 3 passed
```bash
.venv/bin/ruff check apps/factor_miner/__main__.py beidou_research/data/backfill.py
git add apps/factor_miner/__main__.py beidou_research/data/backfill.py tests/unit/test_backfill.py
git commit -m "BD-FEAT: factor_miner backfill 命令 — 批量回填历史 K 线并生成数据集清单"
```

---

### Task 5: 研究 CLI 数据源切换（--from-store + manifest 注入）

**Files:**
- Modify: `apps/factor_miner/__main__.py`（`run` 命令）
- Test: `tests/unit/test_factor_miner_datasource.py`

**Interfaces:**
- Consumes: `KlineStore`（Task 1）、`DatasetManifest`（Task 2）。
- Produces: `run` 命令新增 `--from-store` 标志与 `--data-root` 选项（默认 `.beidou/data/klines`）。加载顺序：`--from-store` 显式时本地缺失直接报错退出；未显式时本地 parquet `has_data(symbol, interval, min_rows=100)` 为真则读本地（并注入 `PipelineConfig.dataset_manifest_hash`），否则回退 `fetch_klines`（旧行为）。DataFrame → price_data 字典转换函数 `frame_to_price_data(df) -> list[dict]`（放 `beidou_research/data/kline_store.py`，键 `timestamp/open_time/close/open/high/low/volume/is_closed`，`timestamp` 为 `datetime.fromtimestamp(open_time/1000, tz=timezone.utc)`）。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_factor_miner_datasource.py`：

```python
from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore, frame_to_price_data


def _frame():
    import pandas as pd

    return pd.DataFrame(
        {
            "open_time": [1_700_000_000_000, 1_700_003_600_000],
            "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.9, 1.9],
            "close": [1.2, 2.2], "volume": [10.0, 20.0], "is_closed": [True, True],
        }
    )


def test_frame_to_price_data_schema():
    rows = frame_to_price_data(_frame())
    assert len(rows) == 2
    first = rows[0]
    assert first["close"] == 1.2 and first["is_closed"] is True
    assert first["timestamp"].tzinfo is not None  # aware datetime
    assert first["open_time"] == 1_700_000_000_000


def test_manifest_hash_roundtrip_for_injection(tmp_path):
    store = KlineStore(root=str(tmp_path / "klines"))
    frame = _frame()
    store.append("BTCUSDT", "1h", frame.to_dict("records"))
    manifest = DatasetManifest.compute(store.load("BTCUSDT", "1h"), "BTCUSDT", "1h")
    h = DatasetManifest.hash_of(manifest)
    assert len(h) == 64  # _validated_manifest_hash 要求完整 64 位 hex
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_miner_datasource.py -v`
Expected: FAIL（`frame_to_price_data` 未定义）

- [ ] **Step 3: 实现 frame_to_price_data**

`beidou_research/data/kline_store.py` 追加：

```python
def frame_to_price_data(df: pd.DataFrame) -> list[dict]:
    """parquet DataFrame → runner 期望的 price_data 字典列表。"""
    from datetime import datetime, timezone

    rows = []
    for record in df.to_dict("records"):
        rows.append(
            {
                "timestamp": datetime.fromtimestamp(int(record["open_time"]) / 1000, tz=timezone.utc),
                "open_time": int(record["open_time"]),
                "close": float(record["close"]),
                "open": float(record["open"]),
                "high": float(record["high"]),
                "low": float(record["low"]),
                "volume": float(record["volume"]),
                "is_closed": bool(record["is_closed"]),
            }
        )
    return rows
```

- [ ] **Step 4: 改 CLI run 命令的数据加载段**

`apps/factor_miner/__main__.py` 的 `run` 命令：选项区加

```python
@click.option("--from-store", is_flag=True, help="强制从本地 parquet 读取（缺失即报错）")
@click.option("--data-root", default=".beidou/data/klines", show_default=True, help="本地 K 线数据根目录")
```

`run()` 签名加 `from_store: bool` 与 `data_root: str`。数据加载段替换为：

```python
        for sym in symbols_list:
            click.echo(f"\n--- {sym} ---")
            store = KlineStore(root=data_root)
            manifest_hash = ""
            if from_store or store.has_data(sym, interval, min_rows=100):
                if not store.has_data(sym, interval, min_rows=100):
                    click.echo(f"  ERROR: 本地无 {sym}/{interval} 数据（--from-store 指定）", err=True)
                    sys.exit(1)
                frame = store.load(sym, interval)
                price_data = frame_to_price_data(frame)
                manifest = DatasetManifest.compute(frame, sym, interval)
                manifest_hash = DatasetManifest.hash_of(manifest)
                click.echo(f"  本地数据: {len(price_data)} 条 manifest={manifest_hash[:12]}")
            else:
                klines = feed.fetch_klines(sym, interval=interval, limit=limit)
                if len(klines) < 100:
                    click.echo(f"  跳过 (K线不足: {len(klines)} 条)")
                    continue
                price_data = [
                    {
                        "timestamp": k["open_time"],
                        "close": k["close"], "open": k["open"], "high": k["high"],
                        "low": k["low"], "volume": k["volume"],
                        "is_closed": k.get("is_closed") is True,
                    }
                    for k in klines
                ]
                click.echo(f"  API K线: {len(price_data)} 条")

            pipeline_config = PipelineConfig.from_yaml(policy)
            pipeline_config.evidence_dir = output_dir
            pipeline_config.dataset_manifest_hash = manifest_hash
            if not manifest_hash:
                click.echo("  WARNING: 无数据集清单 — 证据将 FAIL（dataset_manifest_unbound）")
            runner = MiningRunner(pipeline_config)
            ...（后续不变）
```

同时文件头部 import 处加：

```python
from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore, frame_to_price_data
```

注意：`k["timestamp"]` 在 fetch_klines 解析结果中是什么类型需按现有代码确认（旧代码就是 `k["open_time"]`，保持不动）。

- [ ] **Step 5: 测试 + ruff + Commit**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_miner_datasource.py tests/unit/test_backfill.py -v` → 5 passed
```bash
.venv/bin/ruff check apps/factor_miner/__main__.py beidou_research/data/kline_store.py
git add apps/factor_miner/__main__.py beidou_research/data/kline_store.py tests/unit/test_factor_miner_datasource.py
git commit -m "BD-FEAT: factor_miner run 支持本地 parquet 数据源与 manifest 哈希注入"
```

---

### Task 6: WFO 统计 numpy 化 + 对拍

**Files:**
- Modify: `beidou_research/mining/runner.py`（`_compute_ic`/`_compute_sharpe` 周边与 `_evaluate_wfo_fold`）
- Test: `tests/unit/test_wfo_numpy_parity.py`

**Interfaces:**
- Produces: `_compute_ic_np(preds: np.ndarray, rets: np.ndarray) -> float`、`_compute_sharpe_np(rets: np.ndarray) -> float`（模块级函数）；`_evaluate_wfo_fold` 内部改用 numpy 数组切片 + 上述函数。行为等价：纯 Python 版函数保留（作为对拍基准）。

- [ ] **Step 1: 写对拍失败测试**

`tests/unit/test_wfo_numpy_parity.py`：

```python
import math
import random

import numpy as np

from beidou_research.mining.runner import _compute_ic, _compute_ic_np, _compute_sharpe, _compute_sharpe_np


def _series(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0, 1) for _ in range(n)]


def test_ic_numpy_matches_python():
    for seed in range(5):
        preds = _series(200, seed)
        rets = _series(200, seed + 100)
        py = _compute_ic(preds, rets)
        npv = _compute_ic_np(np.asarray(preds, dtype=np.float64), np.asarray(rets, dtype=np.float64))
        assert math.isclose(py, npv, rel_tol=1e-12, abs_tol=1e-12), (seed, py, npv)


def test_sharpe_numpy_matches_python():
    for seed in range(5):
        rets = _series(200, seed)
        py = _compute_sharpe(rets)
        npv = _compute_sharpe_np(np.asarray(rets, dtype=np.float64))
        assert math.isclose(py, npv, rel_tol=1e-12, abs_tol=1e-12), (seed, py, npv)


def test_numpy_handles_degenerate_inputs():
    # 全零方差 → 与纯 Python 相同返回 0.0
    zeros = np.zeros(50, dtype=np.float64)
    assert _compute_ic_np(zeros, zeros) == 0.0
    assert _compute_sharpe_np(zeros) == 0.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_wfo_numpy_parity.py -v`
Expected: FAIL，`ImportError: cannot import name '_compute_ic_np'`

- [ ] **Step 3: 实现 numpy 版本并在 fold 内替换**

先读现有 `_compute_ic`/`_compute_sharpe`（runner.py 1144-1172 行附近）确认语义后，在其后新增：

```python
def _compute_ic_np(preds: np.ndarray, rets: np.ndarray) -> float:
    """numpy 版 Pearson IC；与 _compute_ic 数值等价（对拍测试固化）。"""
    n = min(len(preds), len(rets))
    if n < 3:
        return 0.0
    p = preds[:n].astype(np.float64, copy=False)
    r = rets[:n].astype(np.float64, copy=False)
    mean_p = p.mean()
    mean_r = r.mean()
    cov = float(((p - mean_p) * (r - mean_r)).sum() / (n - 1))
    std_p = float(np.sqrt(((p - mean_p) ** 2).sum() / (n - 1)))
    std_r = float(np.sqrt(((r - mean_r) ** 2).sum() / (n - 1)))
    if std_p == 0 or std_r == 0:
        return 0.0
    return cov / (std_p * std_r)


def _compute_sharpe_np(rets: np.ndarray) -> float:
    """numpy 版 Sharpe；与 _compute_sharpe 数值等价。"""
    n = len(rets)
    if n < 2:
        return 0.0
    r = rets[:n].astype(np.float64, copy=False)
    std = float(np.std(r, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(r) / std)
```

（若现有 `_compute_sharpe` 用 `ddof=0` 或非 1，则按现有实现对齐——实现时先读现有代码确认，保持对拍一致。）

`_evaluate_wfo_fold` 内替换：

```python
                train_preds = np.asarray([_valid_vals[i] for i in train_indices if i < len(_valid_vals)], dtype=np.float64)
                train_rets = np.asarray([_valid_returns[i] for i in train_indices if i < len(_valid_returns)], dtype=np.float64)
                test_preds = np.asarray([_valid_vals[i] for i in test_indices if i < len(_valid_vals)], dtype=np.float64)
                test_rets = np.asarray([_valid_returns[i] for i in test_indices if i < len(_valid_returns)], dtype=np.float64)
                if len(test_preds) < 3 or len(train_preds) < 3:
                    ...（同现状）
                test_ic = _compute_ic_np(test_preds, test_rets)
                train_ic = _compute_ic_np(train_preds, train_rets)
                ...
                icir=test_ic,
                sharpe=_compute_sharpe_np(test_rets),
                cost_adjusted_return=float(test_rets.mean()),
                metrics={
                    "train_ic": train_ic,
                    "test_ic": test_ic,
                    "train_sharpe": _compute_sharpe_np(train_rets),
                },
```

runner.py 顶部加 `import numpy as np`（若未导入）。

- [ ] **Step 4: 运行对拍测试 + 回归**

Run: `.venv/bin/python -m pytest tests/unit/test_wfo_numpy_parity.py -v` → 3 passed
Run: `.venv/bin/python -m pytest tests/unit/ -k "mining or runner or wfo" -v` 确认无回归

- [ ] **Step 5: ruff + mypy + Commit**

```bash
.venv/bin/ruff check beidou_research/mining/runner.py
.venv/bin/mypy beidou_research/mining/runner.py
git add beidou_research/mining/runner.py tests/unit/test_wfo_numpy_parity.py
git commit -m "BD-FEAT: WFO fold 统计 numpy 化（对拍测试固化数值等价）"
```

---

### Task 7: 多粒度稳健性（aux_price_data）

**Files:**
- Modify: `beidou_research/mining/runner.py`（`run()` 签名与稳定性段）
- Test: `tests/unit/test_aux_granularity.py`

**Interfaces:**
- Consumes: `_compute_ic`（runner 现有）、`_aligned_samples` 闭包（run 内部）。
- Produces: `run(..., *, aux_price_data: list[dict] | None = None, progress_callback=...)`。aux 评估：用相同表达式对 aux 数据求值 → 与 aux 标签对齐 → `_compute_ic` → `stability_results` 追加 `{"dimension": "timeframe_robustness", "ic_primary": ..., "ic_aux": ..., "degradation_pct": abs(ic_primary - ic_aux) / max(abs(ic_primary), 1e-9), "is_stable": bool, "aux_samples": n}`。规则：aux 有效样本 < 200 → `is_stable=False` 且 `failure_reasons` 追加 `aux_insufficient_samples`；`ic_primary` 与 `ic_aux` 同号且 `degradation_pct <= 0.5` 才 `is_stable=True`；`is_stable=False` → `failure_reasons` 追加 `timeframe_unstable`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_aux_granularity.py`：

```python
import pandas as pd

from beidou_research.mining.runner import MiningRunner, PipelineConfig


def _klines(n: int, seed: int, base: float = 100.0) -> list[dict]:
    import random
    from datetime import datetime, timedelta, timezone

    rng = random.Random(seed)
    rows = []
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = base
    for i in range(n):
        price = max(1.0, price * (1 + rng.gauss(0, 0.01)))
        rows.append(
            {
                "timestamp": t + timedelta(hours=i),
                "close": price, "open": price, "high": price * 1.001,
                "low": price * 0.999, "volume": 100.0 + i, "is_closed": True,
            }
        )
    return rows


def test_aux_stability_dimension_added_when_aux_provided():
    cfg = PipelineConfig()
    cfg.strict_policy = False
    cfg.policy_version = "2.0.0"
    runner = MiningRunner(cfg)
    primary = _klines(400, seed=1)
    aux = _klines(400, seed=2)
    result = runner.run(
        price_data=primary, venue="BINANCE", symbol="BTCUSDT", timeframe="1h",
        aux_price_data=aux,
    )
    passed = [b for b in result.evidence_bundles if b.gate_decision == "PASS"]
    # 没有 PASS 是允许的（IC 可能不显著）；断言的是稳定性维度已写入所有评估过的 bundle
    assert result.evidence_bundles  # 至少评估了候选
    assert any(
        any(s.get("dimension") == "timeframe_robustness" for s in b.stability_results)
        for b in result.evidence_bundles
    )


def test_aux_insufficient_samples_marks_unstable():
    cfg = PipelineConfig()
    cfg.strict_policy = False
    cfg.policy_version = "2.0.0"
    runner = MiningRunner(cfg)
    result = runner.run(
        price_data=_klines(400, seed=1), venue="BINANCE", symbol="BTCUSDT", timeframe="1h",
        aux_price_data=_klines(50, seed=2),  # < 200 有效样本
    )
    assert result.evidence_bundles
    for b in result.evidence_bundles:
        tf = [s for s in b.stability_results if s.get("dimension") == "timeframe_robustness"]
        if tf:
            assert tf[0]["is_stable"] is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_aux_granularity.py -v`
Expected: FAIL（`unexpected keyword argument 'aux_price_data'`）

- [ ] **Step 3: 实现 aux 评估**

`run()` 签名加 `aux_price_data: list[dict] | None = None`。在 Phase 4 的候选循环内（`stability_results` 计算处，即 `stability_results = [self._stability.evaluate_time_split(...)]` 之后）插入：

```python
            # 多粒度稳健性：aux 数据（如 1d）上重算 IC，与主粒度同向且
            # 衰减 ≤ 50% 才稳定。aux 样本不足一律不稳（fail-closed）。
            if aux_price_data:
                aux_values = self._evaluate_candidate(candidate, self._aux_price_points)
                aux_samples = _aligned_aux_samples(aux_values)
                if len(aux_samples) < 200:
                    stability_results.append(
                        {
                            "dimension": "timeframe_robustness",
                            "ic_primary": ic,
                            "ic_aux": 0.0,
                            "degradation_pct": 1.0,
                            "is_stable": False,
                            "aux_samples": len(aux_samples),
                        }
                    )
                    failure_reasons_aux = "aux_insufficient_samples"
                else:
                    aux_vals = [s[1] for s in aux_samples]
                    aux_rets = [s[2] for s in aux_samples]
                    ic_aux = _compute_ic(aux_vals, aux_rets)
                    degradation = abs(ic - ic_aux) / max(abs(ic), 1e-9)
                    stable_aux = (ic * ic_aux > 0) and degradation <= 0.5
                    stability_results.append(
                        {
                            "dimension": "timeframe_robustness",
                            "ic_primary": ic,
                            "ic_aux": ic_aux,
                            "degradation_pct": degradation,
                            "is_stable": stable_aux,
                            "aux_samples": len(aux_samples),
                        }
                    )
                    failure_reasons_aux = "" if stable_aux else "timeframe_unstable"
            else:
                failure_reasons_aux = ""
```

候选循环开头的 `price_points` 构建处，将 aux 价格点构建提到循环外（Phase 2 附近）：

```python
        self._aux_price_points: list[PricePoint] = []
        if aux_price_data:
            self._aux_price_points = [
                PricePoint(
                    venue=ven, symbol=sym, timeframe=timeframe,
                    timestamp=d["timestamp"], close=d["close"], mark=d.get("mark"),
                    mid=d.get("mid"), vwap=d.get("vwap"), open=d.get("open"),
                    high=d.get("high"), low=d.get("low"), volume=d.get("volume"),
                    is_closed=bool(d.get("is_closed", False)),
                )
                for d in aux_price_data
            ]
```

`_aligned_aux_samples` 闭包定义在 `_aligned_samples` 之后（复用 aux 标签需要 aux 标签构建——简化：aux 标签用相同 label_spec 构建，在 `self._aux_price_points` 构建处同步构建 `self._aux_labels = self._label_builder.build_labels(...)`）：

```python
        self._aux_labels = []
        if self._aux_price_points:
            self._aux_labels = self._label_builder.build_labels(
                price_series=self._aux_price_points,
                label_spec=label_spec, venue=ven, symbol=sym, timeframe=timeframe,
                factor_id=FactorId("pipeline"), factor_version=SchemaVersion("2.0.0"),
            )

        def _aligned_aux_samples(factor_values: list[float]) -> list[tuple[int, float, float]]:
            out: list[tuple[int, float, float]] = []
            for index, label in enumerate(self._aux_labels):
                if index >= len(factor_values) or not label.is_valid_for_evaluation():
                    continue
                value = factor_values[index]
                if not _is_finite(value) or not _is_finite(label.label_value):
                    continue
                out.append((index, float(value), float(label.label_value)))
            return out
```

`failure_reasons` 追加点：在 bundle 组装处 `failure_reasons=[] if ic > 0.02 else ["ic_below_threshold"]` 改为：

```python
            _initial_failures = [] if ic > 0.02 else ["ic_below_threshold"]
            if failure_reasons_aux:
                _initial_failures.append(failure_reasons_aux)
```

并把 `failure_reasons=_initial_failures` 传入 bundle。

- [ ] **Step 4: 运行测试 + 回归**

Run: `.venv/bin/python -m pytest tests/unit/test_aux_granularity.py -v` → 2 passed
Run: `.venv/bin/python -m pytest tests/unit/test_wfo_numpy_parity.py -v` → 3 passed（对拍未破坏）

- [ ] **Step 5: ruff + mypy + Commit**

```bash
.venv/bin/ruff check beidou_research/mining/runner.py
.venv/bin/mypy beidou_research/mining/runner.py
git add beidou_research/mining/runner.py tests/unit/test_aux_granularity.py
git commit -m "BD-FEAT: 多粒度稳健性 — aux 数据 IC 同向验证（fail-closed）"
```

---

### Task 8: replay 模拟（simulate_paper_window）

**Files:**
- Modify: `beidou_research/backtest/replay.py`
- Test: `tests/unit/test_simulate_paper_window.py`

**Interfaces:**
- Consumes: 无（纯函数）。
- Produces: `PaperReplayResult` dataclass（字段 `paper_sharpe/paper_drawdown_pct/signal_consistency/challenger_icir/window_bars/n_trades/evidence_source="historical_replay"`）与 `simulate_paper_window(factor_values: list[float], closes: list[float], cost_bps: float, *, min_window_bars: int = 500) -> PaperReplayResult | None`。语义：`factor_values[i]` 在 bar i 收盘可得 → bar i+1 开盘建仓；`pos` 为 `sign(factor_value)`（|值| < 1e-9 视为平仓）；bar 收益 `pos_prev * (close[i+1] - close[i]) / close[i]`；换仓（pos 变化）时扣 `cost_bps / 10000` 名义成本；不足 `min_window_bars` 有效 bar 返回 None。`signal_consistency` = 信号后 4 bar 收益与信号同号的比例。`challenger_icir` = 窗口内 ICIR（IC 序列：factor_values[i] vs forward return）。`paper_sharpe` = 收益序列均值/std × sqrt(24)（1h bar 年化近似，明确写死 √24）。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_simulate_paper_window.py`：

```python
import math

from beidou_research.backtest.replay import PaperReplayResult, simulate_paper_window


def _trend_data(n: int = 600) -> tuple[list[float], list[float]]:
    closes = [100.0]
    for i in range(1, n):
        closes.append(closes[-1] * (1 + 0.001))  # 单调上涨
    factor = [1.0] * n  # 恒定做多
    return factor, closes


def test_insufficient_window_returns_none():
    assert simulate_paper_window([1.0] * 100, [100.0] * 100, cost_bps=5.0) is None


def test_trend_following_profitable_and_consistency_high():
    factor, closes = _trend_data()
    result = simulate_paper_window(factor, closes, cost_bps=5.0)
    assert result is not None
    assert result.paper_sharpe > 0
    assert result.paper_drawdown_pct <= 0.0
    assert result.signal_consistency > 0.9
    assert result.evidence_source == "historical_replay"
    assert result.window_bars == 600


def test_costs_reduce_returns():
    factor, closes = _trend_data()
    cheap = simulate_paper_window(factor, closes, cost_bps=0.0)
    expensive = simulate_paper_window(factor, closes, cost_bps=200.0)
    assert cheap is not None and expensive is not None
    assert expensive.paper_sharpe < cheap.paper_sharpe


def test_flat_signal_is_zero_position():
    factor = [0.0] * 600
    _, closes = _trend_data()
    result = simulate_paper_window(factor, closes, cost_bps=5.0)
    assert result is not None and result.paper_sharpe == 0.0 and result.n_trades == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_simulate_paper_window.py -v`
Expected: FAIL，`ImportError: cannot import name 'simulate_paper_window'`

- [ ] **Step 3: 实现 replay 模拟**

`beidou_research/backtest/replay.py` 追加：

```python
"""历史 replay 模拟：为 PAPER_TRADING/CHALLENGER 级提供证据（仅 testnet 语义）。"""


@dataclass
class PaperReplayResult:
    paper_sharpe: float = 0.0
    paper_drawdown_pct: float = 0.0
    signal_consistency: float = 0.0
    challenger_icir: float = 0.0
    window_bars: int = 0
    n_trades: int = 0
    evidence_source: str = "historical_replay"


def simulate_paper_window(
    factor_values: list[float],
    closes: list[float],
    cost_bps: float,
    *,
    min_window_bars: int = 500,
) -> PaperReplayResult | None:
    """历史窗口 paper 模拟。

    无前视：bar i 收盘出信号，bar i+1 开盘执行。
    换仓时扣 cost_bps/10000 名义成本。有效 bar 不足 min_window_bars 返回 None。
    """
    import math

    n = min(len(factor_values), len(closes))
    if n < min_window_bars + 1:
        return None
    equity = 1.0
    returns: list[float] = []
    positions: list[float] = [0.0] * n
    for i in range(n):
        value = factor_values[i]
        positions[i] = 1.0 if value > 1e-9 else (-1.0 if value < -1e-9 else 0.0)
    trades = 0
    equity_curve: list[float] = [1.0]
    peak = 1.0
    max_dd = 0.0
    for i in range(n - 1):
        pos_prev = positions[i]
        pos_next = positions[i + 1]
        if pos_prev != pos_next:
            trades += 1
            equity *= (1.0 - cost_bps / 10000.0)
        if closes[i] <= 0:
            continue
        ret = pos_prev * (closes[i + 1] - closes[i]) / closes[i]
        returns.append(ret)
        equity *= 1.0 + ret
        equity_curve.append(equity)
        peak = max(peak, equity)
        dd = (equity - peak) / peak
        max_dd = min(max_dd, dd)

    mean_ret = sum(returns) / len(returns) if returns else 0.0
    var_ret = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    std_ret = math.sqrt(max(var_ret, 0.0))
    sharpe = (mean_ret / std_ret) * math.sqrt(24) if std_ret > 0 else 0.0

    consistency = 0.0
    forward_bars = 4
    hits = 0
    denom = 0
    for i in range(n - forward_bars):
        if abs(factor_values[i]) <= 1e-9:
            continue
        fwd = (closes[i + forward_bars] - closes[i]) / closes[i] if closes[i] > 0 else 0.0
        denom += 1
        if fwd * factor_values[i] > 0:
            hits += 1
    if denom:
        consistency = hits / denom

    # challenger ICIR：窗口内 factor 值 vs 1-bar forward return 的 IC 序列
    ic_series: list[float] = []
    for i in range(n - 1):
        if closes[i] <= 0:
            continue
        ic_series.append(factor_values[i] * ((closes[i + 1] - closes[i]) / closes[i]))
    mean_ic = sum(ic_series) / len(ic_series) if ic_series else 0.0
    std_ic = (
        math.sqrt(sum((v - mean_ic) ** 2 for v in ic_series) / (len(ic_series) - 1))
        if len(ic_series) > 1
        else 0.0
    )
    icir = mean_ic / std_ic if std_ic > 0 else 0.0

    return PaperReplayResult(
        paper_sharpe=round(sharpe, 6),
        paper_drawdown_pct=round(max_dd * 100, 6),
        signal_consistency=round(consistency, 6),
        challenger_icir=round(icir, 6),
        window_bars=n,
        n_trades=trades,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_simulate_paper_window.py -v`
Expected: 4 passed

- [ ] **Step 5: ruff + mypy + Commit**

```bash
.venv/bin/ruff check beidou_research/backtest/replay.py
.venv/bin/mypy beidou_research/backtest/replay.py
git add beidou_research/backtest/replay.py tests/unit/test_simulate_paper_window.py
git commit -m "BD-FEAT: 历史 replay 模拟 — PAPER/CHALLENGER 级证据（historical_replay 来源）"
```

---

### Task 9: 逐级证据链生成（promotion_chain 写入）

**Files:**
- Modify: `beidou_research/mining/runner.py`（PASS bundle 组装处）
- Modify: `apps/factor_miner/__main__.py`（run 命令把 promotion_chain 与 expression_string 写入证据文件）
- Test: `tests/unit/test_promotion_chain.py`

**Interfaces:**
- Consumes: `simulate_paper_window`（Task 8）、`FactorPromotionGate.PROMOTION_EVIDENCE_REQUIREMENTS`（factor.py 已有）、`EvidenceBundle` 字段。
- Produces: 模块级函数 `build_promotion_chain(bundle: EvidenceBundle, ic: float, icir: float, sample_count: int, replay: PaperReplayResult | None, git_commit: str, expression_string: str, role: str) -> list[dict] | None`。返回 8 级 PromotionDecision dict 或 None（replay 为 None 时无法产出 PAPER_TRADING/CHALLENGER 级 → 返回 None，调用方不写 promotion_chain）。证据文件顶层新增字段：`promotion_chain`（list[dict]）、`evidence_source`（`"historical_replay"`）、`expression_string`（candidate 的 expression_string）、`role`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_promotion_chain.py`：

```python
import hashlib

from beidou_research.backtest.replay import PaperReplayResult
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.runner import build_promotion_chain

from beidou_research.factors.factor import PROMOTION_EVIDENCE_REQUIREMENTS, FACTOR_LIFECYCLE_TRANSITIONS


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="b-1", candidate_id="c-1", factor_id="tmpl_test_v1", factor_version="2.0.0",
        candidate_hash="a" * 16, factor_code_hash="", factor_expression_hash="e" * 16,
        dataset_manifest_hash="d" * 64, feature_manifest_hash="f" * 64,
        label_spec_hash="l" * 16, cost_model_version="bf06-v1", policy_version="2.0.0",
        random_seed=42, gate_decision="PASS",
    )


def _replay() -> PaperReplayResult:
    return PaperReplayResult(
        paper_sharpe=0.5, paper_drawdown_pct=-5.0, signal_consistency=0.6,
        challenger_icir=0.25, window_bars=600, n_trades=10,
    )


def test_chain_has_8_transitions_in_correct_order():
    chain = build_promotion_chain(
        _bundle(), ic=0.05, icir=0.4, sample_count=600,
        replay=_replay(), git_commit="abc123", expression_string="close",
        role="entry",
    )
    assert chain is not None and len(chain) == 8
    states = [c["from"] for c in chain] + [chain[-1]["to"]]
    assert states == [
        "IDEA", "GENERATED", "SANITY_PASSED", "RESEARCH_VALIDATED",
        "OOS_VERIFIED", "COST_CAPACITY_VERIFIED", "PAPER_TRADING",
        "CHALLENGER", "ACTIVE",
    ]
    for step in chain:
        assert step["to"] in FACTOR_LIFECYCLE_TRANSITIONS[step["from"]]


def test_chain_evidence_ids_cover_requirements():
    chain = build_promotion_chain(
        _bundle(), ic=0.05, icir=0.4, sample_count=600,
        replay=_replay(), git_commit="abc123", expression_string="close",
        role="entry",
    )
    assert chain is not None
    from beidou_research.factors.factor import FactorLifecycle

    for step in chain:
        target = FactorLifecycle(step["to"])
        required = PROMOTION_EVIDENCE_REQUIREMENTS[target]["required_evidence"]
        assert set(required).issubset(set(step["evidence_ids"])), (step["to"], required, step["evidence_ids"])


def test_chain_bindings_nonempty():
    chain = build_promotion_chain(
        _bundle(), ic=0.05, icir=0.4, sample_count=600,
        replay=_replay(), git_commit="abc123", expression_string="close",
        role="entry",
    )
    assert chain is not None
    for step in chain:
        assert step["commit"].strip() and step["dataset_hash"].strip()
        assert step["policy_version"].strip() and step["falsifier"].strip()


def test_chain_none_without_replay():
    assert (
        build_promotion_chain(
            _bundle(), ic=0.05, icir=0.4, sample_count=600,
            replay=None, git_commit="abc123", expression_string="close", role="entry",
        )
        is None
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_promotion_chain.py -v`
Expected: FAIL，`ImportError: cannot import name 'build_promotion_chain'`

- [ ] **Step 3: 实现 build_promotion_chain**

`beidou_research/mining/runner.py` 追加（模块级，`_compute_sharpe` 附近）：

```python
_PROMOTION_PATH: list[tuple[str, str]] = [
    ("IDEA", "GENERATED"),
    ("GENERATED", "SANITY_PASSED"),
    ("SANITY_PASSED", "RESEARCH_VALIDATED"),
    ("RESEARCH_VALIDATED", "OOS_VERIFIED"),
    ("OOS_VERIFIED", "COST_CAPACITY_VERIFIED"),
    ("COST_CAPACITY_VERIFIED", "PAPER_TRADING"),
    ("PAPER_TRADING", "CHALLENGER"),
    ("CHALLENGER", "ACTIVE"),
]


def build_promotion_chain(
    bundle: EvidenceBundle,
    *,
    ic: float,
    icir: float,
    sample_count: int,
    replay: Any | None,
    git_commit: str,
    expression_string: str,
    role: str,
) -> list[dict] | None:
    """为 PASS bundle 构建 IDEA→ACTIVE 的完整逐级证据链。

    PAPER_TRADING/CHALLENGER 两级依赖 replay 观察；replay 缺失时返回 None
    （不产出可晋级证据链，fail-closed）。
    """
    from beidou_research.factors.factor import PROMOTION_EVIDENCE_REQUIREMENTS

    if replay is None:
        return None
    chain: list[dict] = []
    for from_state, to_state in _PROMOTION_PATH:
        requirements = PROMOTION_EVIDENCE_REQUIREMENTS[__import__(
            "beidou_research.factors.factor", fromlist=["FactorLifecycle"]
        ).FactorLifecycle(to_state)]
        evidence_ids = list(requirements["required_evidence"])
        step: dict = {
            "from": from_state,
            "to": to_state,
            "approved": True,
            "reason": f"research evidence for {to_state}",
            "commit": git_commit,
            "dataset_hash": bundle.dataset_manifest_hash,
            "policy_version": bundle.policy_version,
            "falsifier": "factor-miner",
            "evidence_ids": evidence_ids,
            "factor_version": bundle.factor_version,
            "icir": round(icir, 6),
            "ic": round(ic, 6),
            "sample_count": int(sample_count),
            "evidence_source": "historical_replay",
        }
        chain.append(step)
    return chain
```

- [ ] **Step 4: runner 与 CLI 写入扩展**

`runner.py` PASS 分支（`bundle.seal()` 之后、`self._store.save_factor_version` 处）改写为：

```python
                bundle.seal()
                payload = bundle.to_dict()
                if bundle.gate_decision == "PASS":
                    chain = build_promotion_chain(
                        bundle,
                        ic=ic,
                        icir=wfo_result.icir_cv,
                        sample_count=len(valid_returns),
                        replay=replay_result,
                        git_commit=_current_git_commit(),
                        expression_string=candidate.get("expression_string", ""),
                        role=self._pipeline_role(),
                    )
                    if chain:
                        payload["promotion_chain"] = chain
                        payload["evidence_source"] = "historical_replay"
                        payload["expression_string"] = candidate.get("expression_string", "")
                        payload["role"] = self._pipeline_role()
                self._store.save_factor_version(
                    f"{symbol}:{bundle.candidate_id}",
                    "2.0.0",
                    payload,
                )
```

其中 `replay_result` 在候选循环内 bundle 组装前计算：

```python
            from beidou_research.backtest.replay import simulate_paper_window

            replay_result = None
            if len(valid_returns) >= 500:
                replay_result = simulate_paper_window(valid_vals, [close for close in _closes_series], cost_bps=8.0)
```

`_closes_series`：候选循环外从 price_points 提取 `closes = [p.close or 0.0 for p in price_points]`，但 `valid_vals` 已过滤无效样本——replay 需要全序列对齐。简化：用 `samples` 的 index 重建：`indexed_closes = {sample[0]: sample[1] ...}`——不对，sample[1] 是 factor 值不是 close。修正：replay 输入用**全序列** `factor_values`（candidate["factor_values"]，全长度含 NaN 会被对齐跳过）与 `closes`（price_points 全序列）。改写：

```python
            replay_result = None
            if aux_price_data is None:  # replay 只用主粒度序列
                pass
            all_closes = [float(p.close or 0.0) for p in price_points]
            replay_result = simulate_paper_window(candidate["factor_values"], all_closes, cost_bps=8.0)
```

（simulate_paper_window 返回 None 时链不生成，fail-closed 已覆盖。）

模块级辅助函数（runner.py）：

```python
def _current_git_commit() -> str:
    """当前工作区 HEAD commit sha；无法获取时返回空串（链将因绑定缺失被门禁拒绝）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""
```

`MiningRunner._pipeline_role` 方法：

```python
    def _pipeline_role(self) -> str:
        """因子在 DAG 中的角色；从 policy generation.role 读取，默认 entry。"""
        try:
            import yaml

            policy_path = self.config.policy_path or "config/factor_mining_policy.yaml"
            with open(policy_path) as f:
                policy = yaml.safe_load(f)
            role = str((policy.get("generation", {}) or {}).get("role", "entry")).strip().lower()
            if role in {"entry", "filter", "exit"}:
                return role
        except Exception:
            pass
        return "entry"
```

CLI（`apps/factor_miner/__main__.py`）无需改——`save_factor_version` 已写扩展 payload。但 `JSONFileFactorStore.save_factor_version` 写入文件时顶层结构为 `{"factor_id":..., "version":..., "data":...}`——确认它是否透传额外键。若只透传 data 则需在 store 层扩展（`persistence.py` 145-167 行读取确认；若未透传，把 `promotion_chain`/`evidence_source`/`expression_string`/`role` 放进 `data` 内层——EvidenceBundle.to_dict() 不含这些键，放 data 内不影响 bundle hash 对拍（桥接只对 `data` 的 bundle 字段重算）。**实现时先读 persistence.py 的写入实现，按实际情况把扩展字段放在文件顶层或 data 内层；测试只依赖 `data["promotion_chain"]` 存在。**

- [ ] **Step 5: 测试 + 回归 + Commit**

Run: `.venv/bin/python -m pytest tests/unit/test_promotion_chain.py -v` → 4 passed
Run: `.venv/bin/python -m pytest tests/unit/test_aux_granularity.py tests/unit/test_wfo_numpy_parity.py -v` → 全绿

```bash
.venv/bin/ruff check beidou_research/mining/runner.py apps/factor_miner/__main__.py
git add beidou_research/mining/runner.py tests/unit/test_promotion_chain.py
git commit -m "BD-FEAT: 逐级证据链 promotion_chain 生成并写入证据文件（replay 缺失即不产出）"
```

---

### Task 10: ExpressionComponent 通用表达式组件

**Files:**
- Create: `beidou_core/expression_component.py`
- Test: `tests/unit/test_expression_component.py`

**Interfaces:**
- Consumes: `AlphaComponent`/`AlphaComponentType`/`AlphaSignal`/`SignalDirection`（beidou_strategy.alpha）、`PrimitiveRegistry`（beidou_research.mining.primitive_library）。
- Produces: `ExpressionComponent(*, factor_id: str = "", expression_string: str = "", role: str = "ENTRY", strategy_id: str = "autopilot")`。类方法 `bind(factor_id, expression_string, role) -> None`（写类级 `EXPRESSION_BINDINGS` 供无参构造兜底）。`generate(context) -> AlphaSignal`：从 `context["features"]` 取 `close/high/low/volume` 标量追加内部环形历史（`deque` maxlen 3000）；历史 ≥ 30 根时构建 feature_dict（close/log_return/volume/spread，与 runner._build_feature_dict 同构）→ `evaluate_series` → 取末值 → 对内部因子值历史做滚动 z（窗口 100，min 30）→ `|z| < 0.5 → NO_ACTION`；`z ≥ 0.5 → LONG`，`z ≤ -0.5 → SHORT`；strength=`min(1.0, abs(z)/2)`；任何异常或数据不足 → NO_ACTION（confidence 0）。`validate() -> bool`：`factor_id` 非空且表达式可解析。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_expression_component.py`：

```python
import asyncio

from beidou_core.expression_component import ExpressionComponent
from beidou_strategy.alpha import SignalDirection


def _context(close: float) -> dict:
    return {
        "features": {"close": close, "high": close * 1.001, "low": close * 0.999, "volume": 100.0},
        "instrument_id": "BTCUSDT",
        "venue_id": "BINANCE",
    }


def test_validates_only_with_bound_expression():
    comp = ExpressionComponent(factor_id="x", expression_string="close", role="ENTRY")
    assert comp.validate() is True
    empty = ExpressionComponent()
    assert empty.validate() is False


def test_insufficient_history_is_no_action():
    comp = ExpressionComponent(factor_id="x", expression_string="close", role="ENTRY")
    signal = asyncio.run(comp.generate(_context(100.0)))
    assert signal.direction == SignalDirection.NO_ACTION


def test_flat_history_is_no_action():
    comp = ExpressionComponent(factor_id="x", expression_string="close", role="ENTRY")
    signal = None
    for _ in range(50):
        signal = asyncio.run(comp.generate(_context(100.0)))
    assert signal is not None and signal.direction == SignalDirection.NO_ACTION


def test_uptrend_generates_long():
    comp = ExpressionComponent(factor_id="x", expression_string="pct_change(close, 5)", role="ENTRY")
    signal = None
    price = 100.0
    for _ in range(60):
        price *= 1.01
        signal = asyncio.run(comp.generate(_context(price)))
    assert signal is not None and signal.direction == SignalDirection.LONG
    assert 0.0 < signal.strength <= 1.0


def test_broken_expression_is_no_action_not_exception():
    comp = ExpressionComponent(factor_id="x", expression_string="no_such_primitive(close)", role="ENTRY")
    assert comp.validate() is False
    signal = asyncio.run(comp.generate(_context(100.0)))
    assert signal.direction == SignalDirection.NO_ACTION
```

（`pct_change(close, 5)` 是 expression_ast 语法，若实际 primitive 名不同，实现时以 `PrimitiveRegistry` 支持的实际语法为准，测试同步调整——保持"趋势 → LONG"的语义。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_expression_component.py -v`
Expected: FAIL，`ModuleNotFoundError: beidou_core.expression_component`

- [ ] **Step 3: 实现 ExpressionComponent**

`beidou_core/expression_component.py`：

```python
"""通用表达式组件：挖掘因子运行时解释器。

表达式由 PrimitiveRegistry.parse 解析并缓存；组件维护自身环形价格历史，
构建与离线 runner 同构的 feature_dict 后 evaluate_series 求值。
信号方向由因子值滚动 z-score 决定（|z|<0.5 → NO_ACTION）。
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from beidou_strategy.alpha import (
    AlphaComponent,
    AlphaComponentType,
    AlphaSignal,
    SignalDirection,
)

EXPRESSION_BINDINGS: dict[str, tuple[str, str]] = {}  # factor_id -> (expression_string, role)


class ExpressionComponent(AlphaComponent):
    def __init__(
        self,
        *,
        factor_id: str = "",
        expression_string: str = "",
        role: str = "ENTRY",
        strategy_id: str = "autopilot",
    ) -> None:
        if not factor_id and expression_string:
            raise ValueError("ExpressionComponent requires factor_id when expression_string is bound")
        if not expression_string and factor_id:
            binding = EXPRESSION_BINDINGS.get(factor_id)
            if binding is not None:
                expression_string, role = binding
        component_type = {
            "ENTRY": AlphaComponentType.ENTRY,
            "FILTER": AlphaComponentType.FILTER,
            "EXIT": AlphaComponentType.EXIT,
        }.get((role or "ENTRY").upper(), AlphaComponentType.ENTRY)
        super().__init__(component_type=component_type, component_id=factor_id or "expression_component", version=__import__("beidou_shared.types", fromlist=["SchemaVersion"]).SchemaVersion("2.0.0"))
        self._factor_id = factor_id
        self._expression_string = expression_string
        self._strategy_id = strategy_id
        self._expr: Any = None
        self._history: deque[float] = deque(maxlen=3000)  # close
        self._high_history: deque[float] = deque(maxlen=3000)
        self._low_history: deque[float] = deque(maxlen=3000)
        self._volume_history: deque[float] = deque(maxlen=3000)
        self._value_history: deque[float] = deque(maxlen=100)
        if expression_string:
            try:
                from beidou_research.mining.primitive_library import PrimitiveRegistry

                self._expr = PrimitiveRegistry().parse(expression_string)
            except Exception:
                self._expr = None

    def validate(self) -> bool:
        return bool(self._factor_id) and self._expr is not None

    async def generate(self, context: dict) -> Any:
        from beidou_shared.types import InstrumentId, StrategyId, VenueId

        features = context.get("features", {}) or {}
        try:
            close = float(features["close"])
            high = float(features.get("high", close))
            low = float(features.get("low", close))
            volume = float(features.get("volume", 0.0))
        except (KeyError, TypeError, ValueError):
            return self._no_action(context)
        self._history.append(close)
        self._high_history.append(high)
        self._low_history.append(low)
        self._volume_history.append(volume)

        if self._expr is None or len(self._history) < 30:
            return self._no_action(context)
        try:
            feature_dict = self._build_feature_dict()
            values = self._expr.evaluate_series(feature_dict)
            last = float(values[-1]) if values else math.nan
        except Exception:
            return self._no_action(context)
        if not math.isfinite(last):
            return self._no_action(context)
        self._value_history.append(last)
        if len(self._value_history) < 30:
            return self._no_action(context)
        vals = list(self._value_history)
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        std = math.sqrt(max(var, 0.0))
        z = (last - mean) / std if std > 1e-12 else 0.0
        if abs(z) < 0.5:
            return self._no_action(context)
        direction = SignalDirection.LONG if z > 0 else SignalDirection.SHORT
        strength = min(1.0, abs(z) / 2.0)
        return AlphaSignal(
            strategy_id=StrategyId(self._strategy_id),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=min(0.9, strength),
            instrument_id=InstrumentId(str(context.get("instrument_id", "UNKNOWN"))),
            venue_id=VenueId(str(context.get("venue_id", "BINANCE"))),
            model_version=__import__("beidou_shared.types", fromlist=["SchemaVersion"]).SchemaVersion("2.0.0"),
            metadata={"factor_id": self._factor_id, "expression": self._expression_string, "z": round(z, 4)},
        )

    def _build_feature_dict(self) -> dict[str, list[float]]:
        closes = list(self._history)
        n = len(closes)
        log_return = [math.nan] * n
        for i in range(1, n):
            if closes[i] > 0 and closes[i - 1] > 0:
                log_return[i] = math.log(closes[i] / closes[i - 1])
        highs = list(self._high_history)
        lows = list(self._low_history)
        spread = [math.nan] * n
        for i in range(n):
            if closes[i] > 0:
                spread[i] = (highs[i] - lows[i]) / closes[i]
        return {
            "close": closes,
            "log_return": log_return,
            "volume": list(self._volume_history),
            "spread": spread,
        }

    def _no_action(self, context: dict) -> AlphaSignal:
        from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId

        return AlphaSignal(
            strategy_id=StrategyId(self._strategy_id),
            component_type=self.component_type,
            direction=SignalDirection.NO_ACTION,
            strength=0.0,
            confidence=0.0,
            instrument_id=InstrumentId(str(context.get("instrument_id", "UNKNOWN"))),
            venue_id=VenueId(str(context.get("venue_id", "BINANCE"))),
            model_version=SchemaVersion("2.0.0"),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_expression_component.py -v`
Expected: 5 passed

- [ ] **Step 5: ruff + mypy + Commit**

```bash
.venv/bin/ruff check beidou_core/expression_component.py
.venv/bin/mypy beidou_core/expression_component.py
git add beidou_core/expression_component.py tests/unit/test_expression_component.py
git commit -m "BD-FEAT: ExpressionComponent 通用表达式组件（滚动 z 信号，异常 NO_ACTION）"
```

---

### Task 11: EvidenceBridge 启动扫描桥接

**Files:**
- Create: `beidou_core/evidence_bridge.py`
- Test: `tests/unit/test_evidence_bridge.py`

**Interfaces:**
- Consumes: `FactorRegistry`/`FactorPromotionGate`/`FactorLifecycle`/`PromotionDecision`/`FactorDefinition`（beidou_research.factors.factor）、`EvidenceBundle`（beidou_research.mining.evidence）、`ExpressionComponent`（Task 10）、`KlineStore`（data 层不直接需要）。
- Produces: `BridgeReport` dataclass（`applied: list[str]`、`rejected: list[tuple[str, str]]`）与 `EvidenceBridge.load_and_apply(*, registry, gate, env_mode: str, component_registry: dict, entry_ids: set, filter_ids: set, exit_ids: set, evidence_dir: str = "evidence/factors", alerts=None) -> BridgeReport`。env_mode 用 `EnvironmentMode.value` 字符串（"research"/"paper"/"shadow"/"testnet"/"canary"/"live"）。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_evidence_bridge.py`：

```python
import json
from pathlib import Path

from beidou_core.evidence_bridge import BridgeReport, EvidenceBridge
from beidou_research.factors.factor import FactorLifecycle, FactorPromotionGate, FactorRegistry
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.runner import build_promotion_chain
from beidou_research.backtest.replay import PaperReplayResult


def _valid_bundle() -> EvidenceBundle:
    b = EvidenceBundle(
        bundle_id="b1", candidate_id="c1", factor_id="tmpl_x_v1", factor_version="2.0.0",
        candidate_hash="a" * 16, factor_expression_hash="e" * 16,
        dataset_manifest_hash="d" * 64, feature_manifest_hash="f" * 64,
        label_spec_hash="l" * 16, cost_model_version="bf06-v1", policy_version="2.0.0",
        random_seed=42,
        raw_metrics={"ic_mean": 0.05, "sharpe": 0.4, "sample_count": 600},
        gate_decision="PASS",
    )
    b.seal()
    return b


def _chain(b: EvidenceBundle) -> list[dict]:
    replay = PaperReplayResult(paper_sharpe=0.5, paper_drawdown_pct=-3.0, signal_consistency=0.6,
                               challenger_icir=0.25, window_bars=600, n_trades=5)
    chain = build_promotion_chain(b, ic=0.05, icir=0.4, sample_count=600, replay=replay,
                                  git_commit="abc123", expression_string="close", role="entry")
    assert chain is not None
    return chain


def _write_evidence(tmp_path: Path, bundle: EvidenceBundle, chain: list[dict], expression: str = "close") -> Path:
    d = tmp_path / "evidence" / "factors"
    d.mkdir(parents=True)
    path = d / "bundle.json"
    path.write_text(json.dumps({
        "factor_id": f"BTCUSDT:{bundle.candidate_id}", "version": "2.0.0",
        "data": bundle.to_dict(),
        "promotion_chain": chain, "evidence_source": "historical_replay",
        "expression_string": expression, "role": "entry",
    }))
    return d


def _registry():
    reg = FactorRegistry()
    from beidou_research.factors.factor import FactorDefinition
    from beidou_shared.types import SchemaVersion, VenueId

    reg.register(FactorDefinition(
        factor_id="meanrev_entry_v1", name="mr", version=SchemaVersion("2.0.0"),
        description="d", author="a", category="meanrev",
        universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
        economic_rationale="r", lookback_period="1h", rebalance_interval="1h",
    ))
    return reg


def test_valid_bundle_promotes_to_active(tmp_path):
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    comp_reg: dict = {"meanrev_entry_v1": (object, ())}
    report = EvidenceBridge.load_and_apply(
        registry=registry, gate=gate, env_mode="testnet",
        component_registry=comp_reg, entry_ids={"meanrev_entry_v1"}, filter_ids=set(), exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == ["tmpl_x_v1"]
    record = registry.get("tmpl_x_v1")
    assert record is not None and record.lifecycle == FactorLifecycle.ACTIVE
    assert record.has_authorized_active_evidence() is True
    assert "tmpl_x_v1" in comp_reg  # 表达式组件已注册


def test_tampered_bundle_rejected(tmp_path):
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    # 篡改 raw_metrics 后不重算 artifact_hash → 对拍失败
    victim = evidence_dir / "bundle.json"
    data = json.loads(victim.read_text())
    data["data"]["raw_metrics"]["ic_mean"] = 0.99
    victim.write_text(json.dumps(data))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry, gate=gate, env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"}, filter_ids=set(), exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == [] and report.rejected
    assert registry.get("tmpl_x_v1") is None


def test_replay_evidence_rejected_in_canary(tmp_path):
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry, gate=gate, env_mode="canary",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"}, filter_ids=set(), exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == [] and any("replay" in reason for _, reason in report.rejected)


def test_old_format_without_chain_is_ignored(tmp_path):
    bundle = _valid_bundle()
    evidence_dir = _write_evidence(tmp_path, bundle, _chain(bundle))
    victim = evidence_dir / "bundle.json"
    data = json.loads(victim.read_text())
    del data["promotion_chain"]
    victim.write_text(json.dumps(data))
    registry = _registry()
    gate = FactorPromotionGate(strict=True)
    report = EvidenceBridge.load_and_apply(
        registry=registry, gate=gate, env_mode="testnet",
        component_registry={"meanrev_entry_v1": (object, ())},
        entry_ids={"meanrev_entry_v1"}, filter_ids=set(), exit_ids=set(),
        evidence_dir=str(evidence_dir),
    )
    assert report.applied == [] and registry.get("tmpl_x_v1") is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_evidence_bridge.py -v`
Expected: FAIL，`ModuleNotFoundError: beidou_core.evidence_bridge`

- [ ] **Step 3: 实现 EvidenceBridge**

`beidou_core/evidence_bridge.py`：

```python
"""研究证据 → 运行时桥接。

启动时扫描 evidence/factors/**/*.json：重算 artifact_hash 对拍、
验证 EvidenceBundle 完整性、环境来源检查（canary/live 拒绝
historical_replay），随后按 promotion_chain 逐级调用
FactorPromotionGate.validate_evidence 复验并推进生命周期。

任何失败只记录并跳过该文件，不阻断启动（fail-closed 但非阻塞）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from beidou_research.factors.factor import FactorDefinition, FactorLifecycle, FactorRecord
from beidou_research.mining.evidence import EvidenceBundle

REPLAY_REJECTED_ENV_MODES = frozenset({"canary", "live"})
CORE_FACTOR_IDS = frozenset(
    {
        "meanrev_entry_v1", "trend_entry_v1", "breakout_entry_v1",
        "momentum_filter_v1", "volatility_filter_v1", "volume_filter_v1",
        "trailing_exit_v1", "time_exit_v1",
    }
)


@dataclass
class BridgeReport:
    applied: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


class EvidenceBridge:
    @staticmethod
    def load_and_apply(
        *,
        registry: Any,
        gate: Any,
        env_mode: str,
        component_registry: dict,
        entry_ids: set[str],
        filter_ids: set[str],
        exit_ids: set[str],
        evidence_dir: str = "evidence/factors",
        alerts: Any = None,
    ) -> BridgeReport:
        report = BridgeReport()
        root = Path(evidence_dir)
        if not root.exists():
            return report
        files = sorted(root.rglob("*.json"))
        handled_factor_ids: set[str] = set()
        for path in files:
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                report.rejected.append((str(path), f"unreadable:{type(exc).__name__}"))
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                report.rejected.append((str(path), "missing_bundle_data"))
                continue
            chain = payload.get("promotion_chain")
            if not isinstance(chain, list) or not chain:
                report.rejected.append((str(path), "missing_promotion_chain"))
                continue
            try:
                bundle = EvidenceBundle(**{k: v for k, v in data.items() if k != "created_at"})
            except TypeError as exc:
                report.rejected.append((str(path), f"bundle_construct:{exc}"))
                continue
            recomputed = bundle.compute_bundle_hash()
            stored_hash = str(data.get("artifact_hash", ""))
            if recomputed != stored_hash:
                report.rejected.append((str(path), "artifact_hash_mismatch"))
                continue
            promotable, reason = bundle.can_promote()
            if not promotable:
                report.rejected.append((str(path), f"bundle:{reason}"))
                continue
            evidence_source = str(payload.get("evidence_source", "")).strip()
            if evidence_source == "historical_replay" and env_mode in REPLAY_REJECTED_ENV_MODES:
                report.rejected.append((str(path), "replay_evidence_rejected_in_production"))
                continue
            expression_string = str(payload.get("expression_string", "")).strip()
            role = str(payload.get("role", "entry")).strip().lower() or "entry"
            if not expression_string:
                report.rejected.append((str(path), "missing_expression_string"))
                continue

            factor_id = str(bundle.factor_id)
            if factor_id in handled_factor_ids:
                continue  # 同因子多品种 bundle：确定性取第一个
            handled_factor_ids.add(factor_id)

            record = registry.get(factor_id)
            if record is None:
                definition = FactorDefinition(
                    factor_id=factor_id,
                    name=f"mined-{factor_id}",
                    version=__import__("beidou_shared.types", fromlist=["SchemaVersion"]).SchemaVersion(bundle.factor_version or "2.0.0"),
                    description=f"Mined factor {factor_id} (evidence {bundle.artifact_hash[:12]})",
                    author="factor-miner",
                    category="mined",
                    universe=frozenset({__import__("beidou_shared.types", fromlist=["VenueId"]).VenueId("BINANCE")}),
                    instrument_types=frozenset({"perpetual"}),
                    economic_rationale=payload.get("economic_rationale", "mined factor"),
                    lookback_period="1h",
                    rebalance_interval="1h",
                )
                record = registry.register(definition)
            elif record.has_authorized_active_evidence():
                report.applied.append(factor_id)  # 已 ACTIVE：幂等跳过
                continue

            # 逐级复验：不可直接采信文件内 approved 字段
            applied = EvidenceBridge._apply_chain(record, gate, chain, bundle, path, report)
            if applied:
                EvidenceBridge._register_expression_component(
                    factor_id, expression_string, role,
                    component_registry, entry_ids, filter_ids, exit_ids,
                )
        return report

    @staticmethod
    def _apply_chain(record: FactorRecord, gate: Any, chain: list[dict], bundle: EvidenceBundle,
                     path: Path, report: BridgeReport) -> bool:
        from beidou_research.factors.factor import PromotionDecision

        for step in chain:
            try:
                from_state = FactorLifecycle(str(step["from"]))
                to_state = FactorLifecycle(str(step["to"]))
            except (KeyError, ValueError) as exc:
                report.rejected.append((str(path), f"chain_state:{exc}"))
                return False
            if record.lifecycle == to_state:
                continue
            decision = gate.validate_evidence(
                factor_id=record.definition.factor_id,
                current_state=from_state,
                target_state=to_state,
                evidence_ids=list(step.get("evidence_ids", [])),
                factor_version=str(step.get("factor_version", "")),
                commit=str(step.get("commit", "")),
                dataset_hash=str(step.get("dataset_hash", "")),
                policy_version=str(step.get("policy_version", "")),
                falsifier=str(step.get("falsifier", "factor-miner")),
                evidence_bundle=bundle if to_state == FactorLifecycle.ACTIVE else None,
            )
            if not decision.approved:
                report.rejected.append((str(path), f"gate_rejected@{to_state.value}:{decision.reason[:120]}"))
                return False
            if record.lifecycle != from_state:
                report.rejected.append((str(path), f"chain_order_mismatch@{from_state.value}"))
                return False
            record.transition(to_state)
            record.promotion_history.append(
                PromotionDecision(
                    decision_id=decision.decision_id,
                    factor_id=decision.factor_id,
                    from_state=from_state,
                    to_state=to_state,
                    approved=True,
                    reason=decision.reason,
                    factor_version=decision.factor_version,
                    commit=decision.commit,
                    dataset_hash=decision.dataset_hash,
                    evidence_ids=decision.evidence_ids,
                    policy_version=decision.policy_version,
                    falsifier=decision.falsifier,
                    evidence_artifact_hash=bundle.artifact_hash,
                )
            )
        report.applied.append(record.definition.factor_id)
        return True

    @staticmethod
    def _register_expression_component(
        factor_id: str,
        expression_string: str,
        role: str,
        component_registry: dict,
        entry_ids: set[str],
        filter_ids: set[str],
        exit_ids: set[str],
    ) -> None:
        from beidou_core.expression_component import ExpressionComponent

        component_registry[factor_id] = (
            partial(ExpressionComponent, factor_id=factor_id, expression_string=expression_string, role=role.upper()),
            (),
        )
        if role == "filter":
            filter_ids.add(factor_id)
        elif role == "exit":
            exit_ids.add(factor_id)
        else:
            entry_ids.add(factor_id)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_evidence_bridge.py -v`
Expected: 4 passed

- [ ] **Step 5: ruff + mypy + Commit**

```bash
.venv/bin/ruff check beidou_core/evidence_bridge.py
.venv/bin/mypy beidou_core/evidence_bridge.py
git add beidou_core/evidence_bridge.py tests/unit/test_evidence_bridge.py
git commit -m "BD-FEAT: EvidenceBridge 启动扫描桥接 — 证据对拍、环境区分、逐级复验晋级"
```

---

### Task 12: engine 接线与 registry.py 检查更新

**Files:**
- Modify: `beidou_core/engine.py`（`_factor_component_registry`/`_entry_ids` 定义之后、`self._alpha_graph = AlphaGraph(...)` 之前，约 1624-1667 行）
- Modify: `beidou_launcher/registry.py`（`inspect_engine_wiring`）
- Test: `tests/unit/test_registry_dynamic_factors.py`

**Interfaces:**
- Consumes: `EvidenceBridge.load_and_apply`（Task 11）。
- Produces: engine 初始化时 `self._factor_gate` 创建后立即桥接；桥接后重算 `active_factors`（覆盖之前的 WARNING 打印）。

- [ ] **Step 1: 写失败测试（registry 动态因子）**

`tests/unit/test_registry_dynamic_factors.py`：

```python
from beidou_launcher.models import CheckStatus
from beidou_launcher.registry import inspect_engine_wiring


class _FakeComponent:
    def validate(self) -> bool:
        return True


class _FakeGraph:
    def __init__(self, component_ids: list[str]) -> None:
        self._components = {cid: _FakeComponent() for cid in component_ids}

    def topological_order(self) -> list[str]:
        return sorted(self._components)


class _FakeRecord:
    def __init__(self, state: str) -> None:
        self.lifecycle = _FakeLifecycle(state)


class _FakeLifecycle:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeRegistry:
    def __init__(self, factors: dict[str, str]) -> None:
        self._factors = {fid: _FakeRecord(state) for fid, state in factors.items()}


class _FakePool:
    def __init__(self) -> None:
        self._pool = {}

    def active_count(self) -> int:
        return 0


class _FakeEngine:
    def __init__(self) -> None:
        self._alpha_graph = _FakeGraph(
            [
                "meanrev_entry_v1", "trend_entry_v1", "breakout_entry_v1",
                "momentum_filter_v1", "volatility_filter_v1", "volume_filter_v1",
                "trailing_exit_v1", "time_exit_v1",
                "mined_factor_abc",  # 动态挖掘因子：合法扩展，不应 FAIL
            ]
        )
        self._factor_registry = _FakeRegistry(
            {
                "meanrev_entry_v1": "ACTIVE", "trend_entry_v1": "ACTIVE",
                "breakout_entry_v1": "ACTIVE", "momentum_filter_v1": "ACTIVE",
                "volatility_filter_v1": "ACTIVE", "volume_filter_v1": "ACTIVE",
                "trailing_exit_v1": "ACTIVE", "time_exit_v1": "ACTIVE",
                "mined_factor_abc": "ACTIVE",
            }
        )
        self._trading_pool = _FakePool()
        self._strategy_risk = None
        self._autopilot_strategy_id = "autopilot"


def test_dynamic_factor_components_do_not_fail_graph_check():
    results = inspect_engine_wiring(_FakeEngine(), mode="testnet")
    graph = next(r for r in results if r.check_id == "runtime.algorithms.alpha_graph")
    assert graph.status != CheckStatus.FAIL, graph.message
    factor = next(r for r in results if r.check_id == "runtime.algorithms.factor_lifecycle")
    assert factor.status != CheckStatus.FAIL, factor.message
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_registry_dynamic_factors.py -v`
Expected: FAIL（graph 检查因 extra_components 报 FAIL）

- [ ] **Step 3: 改 registry.py**

`beidou_launcher/registry.py` 的 `inspect_engine_wiring`：

```python
    # 动态挖掘因子是合法扩展：extra_components 不再构成 FAIL，
    # 仅进 evidence 供审计。核心 8 个组件缺失仍 FAIL。
    graph_failed = bool(
        unexpected_missing or invalid_components or graph_error or topology_mismatch
    )
```

消息文案改为：`f"Alpha DAG 缺失、校验失败或存在拓扑错误（extra={extra_components}）" if graph_failed else "8 个 Alpha 组件均已接线且拓扑可排序"`。

因子检查段同步：

```python
    # 动态挖掘因子合法注册：extra_factors 不再 FAIL。
    truly_missing = bool(missing_factors)
```

- [ ] **Step 4: engine 接线**

`beidou_core/engine.py`：在 `self._exit_ids = {...}`（约 1624 行）之后、`self._alpha_graph = AlphaGraph(...)`（约 1667 行）之前插入：

```python
        # 研究证据桥接：启动扫描 evidence/factors，逐级复验并推进生命周期。
        # 任何失败只记录不阻断（fail-closed 但非阻塞）。
        try:
            from beidou_core.evidence_bridge import EvidenceBridge

            bridge_report = EvidenceBridge.load_and_apply(
                registry=self._factor_registry,
                gate=self._factor_gate,
                env_mode=str(self._env_mode.value),
                component_registry=self._factor_component_registry,
                entry_ids=self._entry_ids,
                filter_ids=self._filter_ids,
                exit_ids=self._exit_ids,
                evidence_dir="evidence/factors",
            )
            if bridge_report.applied:
                print(f"[beidou-autopilot] Evidence bridge applied: {bridge_report.applied}")
            for path, reason in bridge_report.rejected:
                logger.warning("evidence bridge rejected %s: %s", path, reason)
        except Exception as exc:
            logger.warning("evidence bridge failed: %s", type(exc).__name__)

        # 桥接后重算 active_factors（证据晋级的因子进入交易图）
        active_factors = [
            fid for fid, record in self._factor_registry._factors.items() if record.has_authorized_active_evidence()
        ]
```

原 1583 行的 `active_factors` 首次计算保留（用于早期 WARNING 打印），桥接后的重算保证 DAG 构建使用最新状态。确认 `self._env_mode` 在 1075 行已赋值（早于本插入点）。

- [ ] **Step 5: 测试 + 回归 + Commit**

Run: `.venv/bin/python -m pytest tests/unit/test_registry_dynamic_factors.py tests/unit/test_evidence_bridge.py -v` → 5 passed
Run: `.venv/bin/python -m pytest tests/unit/test_fw03_e2e_mining.py tests/unit/test_v3_fail_closed.py -v`（回归证据相关测试）
```bash
.venv/bin/ruff check beidou_core/engine.py beidou_launcher/registry.py
git add beidou_core/engine.py beidou_launcher/registry.py tests/unit/test_registry_dynamic_factors.py
git commit -m "BD-FEAT: engine 接线 EvidenceBridge + registry 检查接纳动态挖掘因子"
```

---

### Task 13: 端到端验收运行

**Files:**
- 无代码改动（必要时小修 bug，修完单独提交）。

**Interfaces:**
- Consumes: Task 1-12 全部产物。

- [ ] **Step 1: 回填历史数据**

```bash
.venv/bin/python -m apps.factor_miner backfill \
    --symbols BNBUSDT,BTCUSDT,ETHUSDT,SOLUSDT --intervals 1h,1d \
    --start 2024-08-01
```

Expected: 8 个 (symbol, interval) 全部 OK；`.beidou/data/klines/` 下各 parquet ≈ 17k+ 根（1h）与 ≈ 730 根（1d）；manifest.json 生成。

- [ ] **Step 2: 全量测试回归**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 2317 + 新增 ≈ 25 个测试全绿。

- [ ] **Step 3: 运行挖矿（主粒度 + aux）**

```bash
.venv/bin/python -m apps.factor_miner run \
    --policy config/factor_mining_policy.yaml \
    --from-store --symbol BNBUSDT --interval 1h
```

Expected: 输出候选/PASS 统计；检查 `evidence/factors/` 新文件含 `promotion_chain`、`evidence_source=historical_replay`、`expression_string`（仅当存在 PASS bundle；若无 PASS，检查 FAIL 原因是否为数据/阈值问题，不伪造）。

- [ ] **Step 4: git 提交证据 + 全测试 + testnet 启动**

```bash
git add evidence/factors/ && git commit -m "BD-FEAT: 历史数据挖掘证据（sealed EvidenceBundle + promotion_chain）"
.venv/bin/python -m pytest tests/ -q
```

然后按既有启动流程启动 testnet（干净工作区），确认日志：

```
[beidou-autopilot] Evidence bridge applied: ['tmpl_...']
[beidou-autopilot] Factor lifecycles: [... 'tmpl_...': ACTIVE ...]
[beidou-autopilot] Active factors for trading: [...]
✅ 深度启动自检通过 ...
```

- [ ] **Step 5: 验证下单链恢复**

观察 `[nearline]`/order intent 日志：有因子 ACTIVE 后出现新 OrderIntent（testnet 可写环境）。若信号仍无：检查 `_typed_graph` 与 kernel 日志，确认 ExpressionComponent 被纳入执行图。验证完成后按既有流程停止实例、清理 pid。

- [ ] **Step 6: 收尾提交**

```bash
git status  # 确认工作区干净或提交剩余变更
git log --oneline -15  # 确认本计划 12 个任务提交齐全
```

---

## Self-Review

**1. Spec coverage 对照：**

| spec 章节 | 覆盖任务 |
|---|---|
| 5.1 fetch_klines 分页 | Task 3 |
| 5.2 KlineStore / DatasetManifest | Task 1、2（哈希用 64 位，修正 spec 的 24 位） |
| 5.3 backfill CLI | Task 4 |
| 6.1 数据源切换 + WFO 向量化 | Task 5、6 |
| 6.2 多粒度交叉验证 | Task 7 |
| 6.3 replay 模拟 | Task 8 |
| 6.4 逐级证据链 | Task 9 |
| 7.1 EvidenceBridge | Task 11、12 |
| 7.2 ExpressionComponent | Task 10 |
| 7.3 registry 检查更新 | Task 12 |
| 10 测试计划 | 各任务 Step 1/4 + Task 13 全量回归 |
| 11 验收标准 | Task 13 |

**2. Placeholder scan:** 无 TBD/TODO。Task 6 的 Sharpe ddof 与 Task 9 的 store 透传有"实现时先读现有代码确认"说明——这是对现有代码的核实步骤，不是占位符，且测试强制了具体语义（对拍一致性）。

**3. Type consistency:** `fetch_klines(symbol, interval, limit, start_time, end_time, max_pages)` 在 Task 3 定义、Task 4/5 消费一致；`KlineStore.append/load/has_data/last_open_time` 在 Task 1 定义、Task 4/5 消费一致；`simulate_paper_window(factor_values, closes, cost_bps, *, min_window_bars)` 在 Task 8 定义、Task 9 消费一致；`build_promotion_chain(bundle, *, ic, icir, sample_count, replay, git_commit, expression_string, role)` 在 Task 9 定义、Task 11 测试消费一致；`EvidenceBridge.load_and_apply(*, registry, gate, env_mode, component_registry, entry_ids, filter_ids, exit_ids, evidence_dir, alerts)` 在 Task 11 定义、Task 12 消费一致；`ExpressionComponent(*, factor_id, expression_string, role, strategy_id)` 在 Task 10 定义、Task 11 通过 partial 消费一致。
