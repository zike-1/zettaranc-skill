"""指南针 ``.vdat`` 日线读取（只读）。目前只用来取活跃市值 ``Z_SK0AMV``。

文件在 ``<指南针>/ANALYSE/Data/ChinaStk/Z_SK/day.vdat``，由 ``ZHINANZHEN_DATA_DIR`` 指向
``ChinaStk`` 目录。这是实测出的结构，不是官方格式说明：

- 按证券分块，每块 7048 字节 = 32 字节名称（不足补 0）+ 250 条记录 + 16 字节校验
- 每条记录 28 字节：日期（uint32，YYYYMMDD）+ 开高低收量额（float32）
- 文件靠前的索引区也会出现名称，按记录合法性排除
- 最后一块未写满，用 0 填充

验证依据：解出的序列与 DSH 保存的 242 行 0AMV 逐日匹配（最后一行是盘中快照，收盘不同）。
软件升级后结构可能变化，所以这里严格校验，校验不过就报错，不猜测字段。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import numpy as np

from .core.errors import ErrorCode, ZettarancError

logger = logging.getLogger(__name__)

AMV_CODE = "Z_SK0AMV"

_NAME_SIZE = 32
_RECORDS_PER_BLOCK = 250
_RECORD = np.dtype(
    [
        ("date", "<u4"),
        ("open", "<f4"),
        ("high", "<f4"),
        ("low", "<f4"),
        ("close", "<f4"),
        ("volume", "<f4"),
        ("amount", "<f4"),
    ]
)
_PRICE_TOLERANCE = 1.0  # float32 在十几万的量级上有小数误差


def configured_data_dir() -> Path | None:
    """返回已配置的 ChinaStk 目录；未配置或目录不对时返回 None。"""
    raw = os.environ.get("ZHINANZHEN_DATA_DIR", "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not (root / "Z_SK" / "day.vdat").is_file():
        logger.warning("[compass] ZHINANZHEN_DATA_DIR=%s 下没有 Z_SK/day.vdat，已忽略", raw)
        return None
    return root


def _valid_date(value: int) -> bool:
    month = value // 100 % 100
    day = value % 100
    return 19900101 <= value <= 21001231 and 1 <= month <= 12 and 1 <= day <= 31


def _parse_block(body: bytes) -> list[np.void]:
    """取块内开头连续合法的记录；遇到填充或不合法的数据就停。"""
    usable = len(body) - len(body) % _RECORD.itemsize
    records = np.frombuffer(body[:usable], dtype=_RECORD)
    out: list[np.void] = []
    previous = 0
    for rec in records:
        d = int(rec["date"])
        o, h, low, c = (float(rec[f]) for f in ("open", "high", "low", "close"))
        if not _valid_date(d) or d <= previous:
            break
        if not all(np.isfinite(x) and x > 0 for x in (o, h, low, c)):
            break
        if h + _PRICE_TOLERANCE < low or not (low - _PRICE_TOLERANCE <= o <= h + _PRICE_TOLERANCE):
            break
        if not low - _PRICE_TOLERANCE <= c <= h + _PRICE_TOLERANCE:
            break
        out.append(rec)
        previous = d
    return out


def read_series(path: Path | str, code: str = AMV_CODE) -> list[dict]:
    """读取一个代码的全部日线，按日期升序；一条都解不出来时报错。

    Returns:
        [{date: YYYY-MM-DD, open, high, low, close, volume, amount}]
    """
    raw = Path(path).read_bytes()
    key = code.encode("ascii").ljust(_NAME_SIZE, b"\x00")
    body_size = _RECORDS_PER_BLOCK * _RECORD.itemsize
    by_date: dict[int, np.void] = {}
    pos = raw.find(key)
    while pos >= 0:
        for rec in _parse_block(raw[pos + _NAME_SIZE : pos + _NAME_SIZE + body_size]):
            by_date[int(rec["date"])] = rec
        pos = raw.find(key, pos + 1)

    if not by_date:
        raise ZettarancError(ErrorCode.DATA_SOURCE_ERROR, f"{path} 里解不出 {code}，文件结构可能已变化")

    rows = []
    for d in sorted(by_date):
        rec = by_date[d]
        text = str(d)
        rows.append(
            {
                "date": f"{text[:4]}-{text[4:6]}-{text[6:]}",
                **{f: round(float(rec[f]), 2) for f in ("open", "high", "low", "close", "volume", "amount")},
            }
        )
    return rows


@lru_cache(maxsize=2)
def _read_amv_cached(path: str, mtime_ns: int, size: int) -> tuple[dict, ...]:
    return tuple(read_series(path))


def read_amv_rows() -> list[dict] | None:
    """配置了指南针目录时读活跃市值；未配置返回 None。"""
    root = configured_data_dir()
    if root is None:
        return None
    path = root / "Z_SK" / "day.vdat"
    stat = path.stat()
    return list(_read_amv_cached(str(path), stat.st_mtime_ns, stat.st_size))


__all__ = ["AMV_CODE", "configured_data_dir", "read_amv_rows", "read_series"]
