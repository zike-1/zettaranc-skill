"""把每日选股结果交给 DSH 工作台：写 DSH home 下的 ``report-picks.json``。

工作台后端（bot-api ``report-picks.ts``）每分钟检查这个文件，把名单同步进「今日选股」分组：
不再入选的移出分组，只有它自己新建的行才会删整行，用户自己加的自选不删。
工作台接口只认浏览器登录凭证，所以不从这里调 HTTP。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_GROUP = "今日选股"


def default_picks_file() -> Path | None:
    """DSH_REPORT_PICKS_FILE，或工作台目录（ZT_DSH_HOME，其次 DSH_HOME）下的 report-picks.json；都没配置时不交给工作台。"""
    explicit = os.environ.get("DSH_REPORT_PICKS_FILE", "").strip()
    if explicit:
        return Path(explicit)
    home = os.environ.get("ZT_DSH_HOME", "").strip() or os.environ.get("DSH_HOME", "").strip()
    return Path(home) / "report-picks.json" if home else None


@dataclass
class SyncResult:
    ok: bool = False
    group: str = DEFAULT_GROUP
    path: str = ""
    symbols: list[str] = field(default_factory=list)
    error: str = ""


def to_ts_code(code: str) -> str:
    if "." in code:
        return code
    if code.startswith(("5", "6", "9")) and not code.startswith("92"):
        return f"{code}.SH"
    if code.startswith(("4", "8", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def picks_payload(picks: list[dict], date: str, group: str = DEFAULT_GROUP) -> dict:
    seen: dict[str, str] = {}
    for p in picks:
        seen.setdefault(to_ts_code(p["code"]), p.get("name", ""))
    return {"date": date, "group": group, "picks": [{"symbol": s, "name": n} for s, n in seen.items()]}


def write_picks_file(picks: list[dict], date: str, path: Path | None = None, group: str = DEFAULT_GROUP) -> SyncResult:
    """写入名单（先写临时文件再改名，工作台不会读到半截）。目录不存在说明没装工作台，返回失败原因。"""
    payload = picks_payload(picks, date, group)
    path = path or default_picks_file()
    result = SyncResult(group=group, path=str(path or ""), symbols=[p["symbol"] for p in payload["picks"]])
    if path is None:
        result.error = "没有配置 DSH 工作台（.env 里的 ZT_DSH_HOME 或 DSH_REPORT_PICKS_FILE）"
        return result
    if not path.parent.is_dir():
        result.error = f"工作台目录不存在：{path.parent}"
        return result
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        result.error = f"写入失败：{e}"
        return result
    result.ok = True
    return result
