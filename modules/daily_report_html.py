"""每日选股报告的 HTML 渲染（单文件、无外部依赖，离线可开）。

数据来自 ``daily_selection_report.collect_report``。A 股习惯：红 = 涨 / 多头 / 红砖，绿 = 跌 / 空头 / 绿砖。
"""

from __future__ import annotations

from datetime import datetime
from html import escape

WEEKDAYS = "一二三四五六日"

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#86909c;--line:#e5e6eb;--soft:#f2f3f5;
--up:#e8384f;--up-bg:#fdecee;--down:#1aa260;--down-bg:#e6f6ee;--warn:#d97706;--warn-bg:#fff7e6;
--accent:#3b6fe0;--accent-bg:#ebf1fd;--shadow:0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.04)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121417;--card:#1c1f24;--text:#e6e8eb;
--muted:#8a929c;--line:#2c3036;--soft:#24282e;--up:#ff5c6f;--up-bg:#3a1d22;--down:#35c47c;--down-bg:#16301f;
--warn:#f0a43a;--warn-bg:#35290f;--accent:#6c95f0;--accent-bg:#1c2740;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",
"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:16px}
h1{font-size:24px;margin:0;font-weight:650;letter-spacing:.5px}
.sub{color:var(--muted);font-size:13px}
nav{display:flex;gap:8px;align-items:center;font-size:13px}
nav a,nav select{color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:4px 10px;text-decoration:none;font:inherit}
nav a.off{color:var(--muted);pointer-events:none;opacity:.5}
.notes{display:flex;flex-direction:column;gap:6px;margin-bottom:16px}
.note{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:8px 12px;font-size:13px}
.note.info{background:var(--accent-bg);color:var(--accent)}
section{margin-top:28px}
h2{font-size:17px;margin:0 0 4px;font-weight:650;display:flex;align-items:baseline;gap:8px}
h2 .count{font-size:13px;color:var(--muted);font-weight:400}
.lead{color:var(--muted);font-size:13px;margin:0 0 12px}
.card{background:var(--card);border-radius:14px;box-shadow:var(--shadow);border:1px solid var(--line);padding:14px 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.amv{display:grid;grid-template-columns:minmax(220px,1fr) 2fr;gap:20px;align-items:center}
@media (max-width:640px){.amv{grid-template-columns:1fr}}
.state{display:inline-block;font-size:28px;font-weight:700;padding:2px 16px;border-radius:12px}
.bull{color:var(--up);background:var(--up-bg)}.bear{color:var(--down);background:var(--down-bg)}
.unknown{color:var(--muted);background:var(--soft)}
.amv .meta{margin-top:8px;color:var(--muted);font-size:13px}
.amv .next{font-size:14px;margin-bottom:6px}
.amv svg{width:100%;height:84px;display:block}
.up{color:var(--up)}.down{color:var(--down)}.muted{color:var(--muted)}
.stock-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.stock-name{font-weight:650;font-size:16px}
.code{color:var(--muted);font-size:12px;margin-left:6px;font-variant-numeric:tabular-nums}
.px{font-variant-numeric:tabular-nums;font-size:14px;white-space:nowrap}
.bricks{display:flex;align-items:center;gap:3px;margin:10px 0 8px;font-size:12px;color:var(--muted)}
.brick{width:14px;height:10px;border-radius:2px}
.brick.r{background:var(--up)}.brick.g{background:var(--down)}
.bricks span{margin-left:6px}
.rows{display:flex;flex-direction:column;gap:5px;font-size:13.5px}
.row{display:flex;gap:8px;align-items:baseline}
.row .k{color:var(--muted);flex:0 0 36px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tag{font-size:12.5px;padding:2px 9px;border-radius:999px;background:var(--soft);color:var(--text);line-height:1.6}
.tag.src{background:var(--accent-bg);color:var(--accent)}
.tag.buy{background:var(--up-bg);color:var(--up)}
.tag.sell{background:var(--down-bg);color:var(--down)}
.tag.fly{background:var(--warn-bg);color:var(--warn)}
.tag.ind{background:var(--soft);color:var(--muted)}
.blocks{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.block{font-size:13px;padding:6px 12px;border-radius:10px;background:var(--card);border:1px solid var(--line)}
.block b{font-weight:650}.block .ok{color:var(--down)}.block .stale{color:var(--warn)}
.empty{color:var(--muted);text-align:center;padding:22px 12px;font-size:14px}
details{margin-top:12px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:500}
td.num,th.num{text-align:right}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:12px}
.stat .label{color:var(--muted);font-size:12.5px}
.stat .value{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.stat .hint{color:var(--muted);font-size:12px}
footer{margin-top:40px;color:var(--muted);font-size:12.5px;line-height:1.8}
.scroll{overflow-x:auto}
"""


def _e(value) -> str:
    return escape(str(value), quote=True)


def _pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    cls = "up" if value > 0 else "down" if value < 0 else "muted"
    return f'<span class="{cls}">{value:+.{digits}f}%</span>'


def _date_cn(text: str) -> str:
    """2026-11-03 / 20261103 → 11月3日 周二"""
    digits = text.replace("-", "")
    try:
        d = datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        return text
    return f"{d.month}月{d.day}日 周{WEEKDAYS[d.weekday()]}"


def _ymd(text: str) -> str:
    digits = text.replace("-", "")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) == 8 else text


# ───────────────────────── 组件 ─────────────────────────


def _sparkline(series: list[dict]) -> str:
    """活跃市值走势：按当天多空状态给线段上色。"""
    points = [p for p in series if p.get("close")]
    if len(points) < 2:
        return ""
    width, height, pad = 600, 84, 6
    lo = min(p["close"] for p in points)
    hi = max(p["close"] for p in points)
    span = hi - lo or 1
    step = (width - pad * 2) / (len(points) - 1)

    def xy(i: int, p: dict) -> tuple[float, float]:
        return pad + i * step, height - pad - (p["close"] - lo) / span * (height - pad * 2)

    color = {"BULL": "var(--up)", "BEAR": "var(--down)"}
    segments = []
    for i in range(1, len(points)):
        x1, y1 = xy(i - 1, points[i - 1])
        x2, y2 = xy(i, points[i])
        stroke = color.get(points[i]["state"], "var(--muted)")
        segments.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="2" stroke-linecap="round"/>')
    lx, ly = xy(len(points) - 1, points[-1])
    last_color = color.get(points[-1]["state"], "var(--muted)")
    first, last = _date_cn(points[0]["date"]).split(" ")[0], _date_cn(points[-1]["date"]).split(" ")[0]
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="活跃市值近{len(points)}天走势，红色为多头区间，绿色为空头区间">'
        + "".join(segments)
        + f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="{last_color}"/></svg>'
        f'<div class="sub" style="display:flex;justify-content:space-between"><span>{_e(first)}</span>'
        f'<span>近{len(points)}个交易日 · 红=多头 绿=空头</span><span>{_e(last)}</span></div>'
    )


def _amv_card(amv: dict | None, behind: bool) -> str:
    if not amv:
        return '<div class="card empty">没有读到活跃市值数据（指南针 Z_SK/day.vdat）。</div>'
    label, cls = {"BULL": ("多头", "bull"), "BEAR": ("空头", "bear")}.get(amv["state"], ("未知", "unknown"))
    since = _date_cn(amv["since"]).split(" ")[0] if amv.get("since") else ""
    nexts = []
    if amv.get("bull_at") is not None:
        nexts.append(f'明天涨 <b class="up">≥ {amv["bull_at"]:+.2f}%</b> 转多头')
    if amv.get("bear_at") is not None:
        nexts.append(f'明天跌 <b class="down">≤ {amv["bear_at"]:+.2f}%</b> 转空头')
    stale = f'<div class="note" style="margin-top:8px">活跃市值还停在 {_e(_date_cn(amv["date"]))}，指南针没更新到最新交易日</div>' if behind else ""
    return (
        '<div class="card amv"><div>'
        f'<div class="sub">活跃市值 · {_e(_date_cn(amv["date"]))}</div>'
        f'<div class="state {cls}">{label}</div>'
        f'<div class="meta">第 {amv["days_in_state"]} 天（自 {_e(since)}）· 当日 {_pct(amv["pct_chg"])}</div>'
        f'{stale}</div><div>'
        f'<div class="next">{"　".join(nexts)}</div>'
        f'{_sparkline(amv.get("series", []))}'
        '<div class="sub" style="margin-top:4px">只作大盘背景，不否决选股和加仓。</div>'
        "</div></div>"
    )


def _bricks(view: dict) -> str:
    if view["brick"] == "红砖翻绿":
        return '<div class="bricks"><i class="brick r"></i><i class="brick g"></i><span>红砖翻绿</span></div>'
    count = 0
    kind = ""
    if view["brick_trend"] == "RED":
        count, kind = view["red_bricks"], "r"
    elif view["brick_trend"] == "GREEN":
        digits = "".join(ch for ch in view["brick"] if ch.isdigit())
        count, kind = (int(digits) if digits else 0), "g"
    shown = "".join(f'<i class="brick {kind}"></i>' for _ in range(min(count, 6)))
    more = "…" if count > 6 else ""
    return f'<div class="bricks">{shown}{more}<span>{_e(view["brick"])}</span></div>'


def _head(view: dict, extra: str = "") -> str:
    return (
        '<div class="stock-head"><div>'
        f'<span class="stock-name">{_e(view["name"] or view["code"])}</span><span class="code">{_e(view["code"])}</span>{extra}'
        f'</div><div class="px">{view["close"]:.2f} {_pct(view["pct_chg"])}</div></div>'
    )


def _holding_card(view: dict) -> str:
    if view.get("error"):
        return f'<div class="card">{_head({**view, "close": 0, "pct_chg": None})}<div class="muted">无法分析：{_e(view["error"])}</div></div>'
    add = f'<span class="tag buy">{_e(view["add_verdict"])}</span>' if view["add_verdict"] else '<span class="muted">今日无B1，不加</span>'
    fly = f'<span class="tag fly">{_e(view["flyaway"])}</span>' if view["flyaway"] else '<span class="muted">无</span>'
    sells = "".join(f'<span class="tag sell">{_e(s)}</span>' for s in view["sells_today"])
    earlier = [s for s in view["sells"] if s not in view["sells_today"]]
    if earlier:
        sells += f'<span class="muted" style="font-size:12px">近{5}日还有 {_e("、".join(earlier))}</span>'
    sells = sells or '<span class="muted">无</span>'
    return (
        f'<div class="card">{_head(view)}{_bricks(view)}<div class="rows">'
        f'<div class="row"><span class="k">加仓</span><span>{add}</span></div>'
        f'<div class="row"><span class="k">放飞</span><span>{fly}</span></div>'
        f'<div class="row"><span class="k">卖出</span><span class="tags" style="margin:0">{sells}</span></div>'
        f'<div class="row"><span class="k">BOLL</span><span class="muted">日 {_e(view["boll_day"])} · 周 {_e(view["boll_week"])}</span></div>'
        "</div></div>"
    )


def _pick_card(pick: dict) -> str:
    tags = []
    for reason in pick["reasons"]:
        cls = "src" if reason.endswith("选出") else "buy" if ("B1" in reason) else ""
        tags.append(f'<span class="tag {cls}">{_e(reason)}</span>')
    return f'<div class="card">{_head(pick)}<div class="tags">{"".join(tags)}</div></div>'


def _dividend_card(view: dict) -> str:
    streak = f"连续{view['b1_streak']}天B1" if view["b1_streak"] > 1 else (view["b1_text"] or f"今日{view['b1_today']}")
    return (
        f'<div class="card">{_head(view, f" <span class=\"tag ind\">{_e(view["industry"])}</span>")}'
        f'<div class="tags"><span class="tag buy">{_e(streak)}</span><span class="tag">{_e(view["brick"])}</span></div>'
        f'<div class="sub" style="margin-top:8px">BOLL 日 {_e(view["boll_day"])} · 周 {_e(view["boll_week"])}</div></div>'
    )


def _section(title: str, count: str, lead: str, body: str, anchor: str) -> str:
    count_html = f'<span class="count">{_e(count)}</span>' if count else ""
    lead_html = f'<p class="lead">{lead}</p>' if lead else ""
    return f'<section id="{anchor}"><h2>{_e(title)}{count_html}</h2>{lead_html}{body}</section>'


def _notes(report: dict) -> str:
    notes: list[tuple[str, str]] = []
    behind = report.get("behind_days")
    if behind:
        notes.append(("", f"行情数据最新到 {_ymd(report['data_date'])}，比最近工作日晚 {behind} 个工作日。"))
    overlay = report.get("overlay", {})
    if overlay.get("applied"):
        by_source = "、".join(f"{k} {v} 只" for k, v in (overlay.get("sources") or {}).items())
        notes.append(("info", f"本地通达信还没有 {_ymd(overlay['trade_date'])} 的数据，已用实时行情补齐 {overlay['applied']} 只"
                      + (f"（{_e(by_source)}）" if by_source else "") + "。"))
    if overlay.get("error"):
        notes.append(("", f"实时行情补齐失败：{_e(overlay['error'])}"))
    skipped = overlay.get("skipped") or []
    if skipped:
        sample = "、".join(f"{s['code']}（{s['reason']}）" for s in skipped[:5])
        notes.append(("", f"{len(skipped)} 只没补上当天：{_e(sample)}{'…' if len(skipped) > 5 else ''}"))
    refresh = report.get("amv_refresh")
    if refresh and refresh.get("error"):
        notes.append(("", f"自动更新活跃市值没成功：{_e(refresh['error'])}"))
    dates = report.get("stock_dates") or []
    if len(dates) == 2 and dates[0] != dates[1]:
        notes.append(("", f"各股数据日期不一致：{_ymd(dates[0])} ~ {_ymd(dates[1])}（停牌或未补齐）。"))
    if not notes:
        return ""
    return '<div class="notes">' + "".join(f'<div class="note {cls}">{text}</div>' for cls, text in notes) + "</div>"


def _nav(current: str, days: list[str]) -> str:
    idx = days.index(current) if current in days else len(days) - 1
    prev_day = days[idx - 1] if idx > 0 else ""
    next_day = days[idx + 1] if idx + 1 < len(days) else ""
    options = "".join(
        f'<option value="{_e(d)}.html"{" selected" if d == current else ""}>{_e(d)}</option>' for d in reversed(days)
    )
    return (
        "<nav>"
        f'<a class="{"" if prev_day else "off"}" href="{_e(prev_day)}.html">‹ 前一天</a>'
        f'<select onchange="location.href=this.value" aria-label="选择日期">{options}</select>'
        f'<a class="{"" if next_day else "off"}" href="{_e(next_day)}.html">后一天 ›</a>'
        "</nav>"
    )


def _block_chip(b: dict) -> str:
    """一个选股公式：本地结果只数 + 与通达信一键选股的对照。"""
    if not b.get("known", True):
        return f'<div class="block"><b>{_e(b["name"])}</b> <span class="stale">通达信没开，且本地没有移植这个公式</span></div>'
    tdx = b.get("tdx") or {}
    if tdx.get("missing"):
        compare = '<span class="muted">通达信里没有这个板块</span>'
    elif not tdx.get("fresh"):
        compare = f'<span class="muted">通达信结果是 {_e(tdx.get("updated_at", "")[5:] or "?")} 的，不对照</span>'
    else:
        only_local, only_tdx = tdx.get("only_local", []), tdx.get("only_tdx", [])
        if not only_local and not only_tdx:
            compare = f'<span class="ok">与通达信一致（{tdx["count"]} 只）</span>'
        else:
            parts = []
            if only_local:
                parts.append(f"本地多 {_e('、'.join(only_local))}")
            if only_tdx:
                parts.append(f"通达信多 {_e('、'.join(only_tdx))}")
            compare = f'<span class="stale">与通达信不同：{"；".join(parts)}</span>'
    return f'<div class="block"><b>{_e(b["name"])}</b> {b["count"]} 只 · {compare}</div>'


def _sync_text(sync: dict | None) -> str:
    if not sync:
        return ""
    if not sync.get("ok"):
        return f'<span style="color:var(--warn)">没有交给工作台：{_e(sync.get("error", ""))}</span>'
    return f"名单已交给工作台，一分钟内同步到「{_e(sync['group'])}」分组。"


def _follow_up(follow: dict) -> str:
    entries = follow.get("entries", [])
    if not entries:
        return '<div class="card empty">从今天开始记录入选的票，5 个交易日后出现第一批结果。</div>'
    stats = []
    for n, s in sorted(follow.get("summary", {}).items(), key=lambda kv: int(kv[0])):
        stats.append(
            f'<div class="card stat"><div class="label">入选后 {n} 个交易日</div>'
            f'<div class="value">{_pct(s["avg"])}</div>'
            f'<div class="hint">{s["count"]} 只 · 胜率 {s["win_rate"]:.0f}% · 相对上证 {s["avg_excess"]:+.2f}%</div></div>'
        )
    stats_html = f'<div class="stats">{"".join(stats)}</div>' if stats else ""
    rows = []
    for e in entries[:60]:
        rows.append(
            f"<tr><td>{_e(_ymd(e['date']))}</td><td>{_e(e['name'])} <span class=\"code\">{_e(e['code'])}</span></td>"
            f"<td>{_e('、'.join(e['sources']))}</td>"
            + "".join(f'<td class="num">{_pct(e["returns"].get(n))}</td>' for n in ("5", "10"))
            + f'<td class="num">{_pct(e["bench"].get("5"))}</td></tr>'
        )
    table = (
        '<div class="card scroll"><table><thead><tr><th>入选日</th><th>股票</th><th>来源</th>'
        '<th class="num">5日</th><th class="num">10日</th><th class="num">上证5日</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )
    return stats_html + table


# ───────────────────────── 页面 ─────────────────────────


def render_report(report: dict, days: list[str]) -> str:
    date_text = report["date"]
    holdings = report.get("holdings", [])
    holding_body = (
        f'<div class="grid">{"".join(_holding_card(v) for v in holdings)}</div>'
        if holdings else '<div class="card empty">工作台「持仓」分组是空的。</div>'
    )

    blocks_html = "".join(_block_chip(b) for b in report.get("blocks", []))
    sel = report.get("selection") or {}
    if sel.get("scanned"):
        engine = sel.get("engine") or "本地移植"
        blocks_html += (
            f'<div class="block muted">{_e(engine)}引擎扫描 {sel["scanned"]} 只，取{_e("、".join(sel.get("boards", [])))}'
            + (f'，{sel["errors"]} 只出错' if sel.get("errors") else "") + "</div>"
        )
    if sel.get("note"):
        blocks_html += f'<div class="block"><span class="stale">{_e(sel["note"])}</span></div>'
    picks = report.get("picks", [])
    dropped = report.get("dropped", [])
    pick_cards = (
        f'<div class="grid">{"".join(_pick_card(p) for p in picks)}</div>'
        if picks else '<div class="card empty">今天的选股结果过滤后没有剩下的。</div>'
    )
    dropped_html = ""
    if dropped:
        rows = "".join(
            f"<tr><td>{_e(d['name'] or d['code'])} <span class=\"code\">{_e(d['code'])}</span></td>"
            f"<td>{_e('、'.join(d['sources']))}</td><td>{_e(d['reason'])}</td></tr>"
            for d in dropped
        )
        dropped_html = (
            f'<details><summary>过滤掉 {len(dropped)} 只（点开看原因）</summary><div class="card scroll" style="margin-top:8px">'
            f'<table><thead><tr><th>股票</th><th>来源</th><th>原因</th></tr></thead><tbody>{rows}</tbody></table></div></details>'
        )
    picks_body = f'<div class="blocks">{blocks_html}</div>{pick_cards}{dropped_html}'

    dividend = report.get("dividend")
    dividend_html = ""
    if dividend is not None:
        b1 = dividend["b1"]
        body = (
            f'<div class="grid">{"".join(_dividend_card(v) for v in b1)}</div>'
            if b1 else '<div class="card empty">今天电力、银行、保险里没有 B1。</div>'
        )
        failed = f"，{dividend['failed']} 只无法分析" if dividend.get("failed") else ""
        dividend_html = _section(
            "红利组 · 今日B1", f"{len(b1)} / {dividend['total']}",
            f"电力、银行、保险共 {dividend['total']} 只，只列今天出现 B1 的{failed}。", body, "dividend",
        )

    rules = report.get("rules", {})
    overlay_date = report.get("overlay", {}).get("trade_date")
    source = "通达信本地" + (" + 实时行情补齐" if overlay_date else "")
    title = f"每日选股 {date_text}"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<header><div><h1>每日选股 · {_e(_date_cn(date_text))}</h1>
<div class="sub">行情 {_e(_ymd(report.get("data_date", "")))}（{_e(source)}）· 生成于 {_e(report.get("generated_at", ""))}</div></div>
{_nav(date_text, days)}</header>
{_notes(report)}
{_amv_card(report.get("amv"), report.get("amv_behind", False))}
{_section("持仓", f"{len(holdings)} 只", "加仓只看 B1；放飞和卖出信号按技能规则。", holding_body, "holdings")}
{_section("今日选股", f"{len(picks)} 只", "用通达信选股公式扫描、再经技能过滤后剩下的；每只只写买入理由。" + _sync_text(report.get("dsh_sync")), picks_body, "picks")}
{dividend_html}
{_section("入选后表现", "", "之前每天入选的票，从入选日收盘算起的涨跌（等比复权），对比同期上证指数。", _follow_up(report.get("follow_up", {})), "follow")}
<footer>
过滤规则：{_e(rules.get("exclude", ""))}。<br>
加仓：{_e(rules.get("add", ""))}。活跃市值：{_e(rules.get("amv", ""))}。<br>
以上是 zettaranc 技能规则的机械判断，不是投资建议。
</footer>
</div></body></html>
"""


def render_index(days: list[str]) -> str:
    """index.html：自动跳到最新一天，同时列出所有日期。"""
    latest = days[-1] if days else ""
    links = "".join(f'<li><a href="{_e(d)}.html">{_e(d)} {_e(_date_cn(d).split(" ")[1])}</a></li>' for d in reversed(days))
    redirect = f'<meta http-equiv="refresh" content="0; url={_e(latest)}.html">' if latest else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日选股</title>{redirect}<style>{CSS} li{{margin:6px 0}} a{{color:var(--accent)}}</style></head>
<body><div class="wrap"><h1>每日选股</h1><ul>{links}</ul></div></body></html>
"""
