#!/usr/bin/env python3
"""用知识库训练场的场景卡考技能。

    python scripts/eval_cards.py export --out blind.jsonl    # 导出不含答案的题目
    python scripts/eval_cards.py score answers.csv           # 给答案打分（列：id,answer，从 1 开始的选项号）
    python scripts/eval_cards.py coverage                    # 每张卡引用的规则，技能文档里有没有

题目顺序和选项顺序按固定种子打乱，答案位置和原卡不同，打分时再换算回来。
题库来自粉丝笔记知识库，用环境变量 ZK_WIKI_DIR 指向你自己的副本。题库不进技能目录：版权归原作者。
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import random
import re
import sys
from pathlib import Path

SEED = 20261102
ROOT = Path(__file__).resolve().parent.parent
OPTION = re.compile(r"^(\d+)\.\s*(✅|❌)\s*(.+)$")


def cards_path() -> Path:
    root = os.environ.get("ZK_WIKI_DIR", "").strip()
    if not root:
        raise SystemExit("没有配置 ZK_WIKI_DIR（粉丝笔记知识库副本的目录）")
    return Path(root) / "03_rag" / "game_cards.jsonl"


def parse_card(card: dict) -> dict | None:
    """拆出题干、选项和正确选项。没有选项的卡（比如评分题）返回 None。"""
    text = card["text"]
    if "**选项**" not in text:
        return None
    head, rest = text.split("**选项**", 1)
    head = re.sub(r"\*\*提示\*\*[:：].*", "", head)
    head = re.sub(r"^### .*$|^\*\[.*\]\*$", "", head, flags=re.M)
    options, correct = [], []
    for line in rest.split("**正确答案**")[0].splitlines():
        m = OPTION.match(line.strip())
        if m:
            options.append(m.group(3).strip())
            if m.group(2) == "✅":
                correct.append(len(options) - 1)
    if len(options) < 2 or not correct:
        return None
    return {
        "id": card["id"],
        "chapter": card["chapter"],
        "chapter_title": card.get("chapter_title", ""),
        "type": card.get("type", ""),
        "difficulty": card.get("difficulty", ""),
        "stem": re.sub(r"\n{3,}", "\n\n", head).strip(),
        "options": options,
        "correct": correct,
    }


def load_cards() -> tuple[list[dict], int]:
    raw = [json.loads(line) for line in cards_path().open(encoding="utf-8")]
    raw = [c for c in raw if c.get("chapter") != "oracle" and c.get("type") != "oracle"]
    parsed = [parse_card(c) for c in raw]
    return [c for c in parsed if c], sum(1 for c in parsed if c is None)


def shuffled(card: dict) -> tuple[list[str], list[int]]:
    """按卡片 id 固定打乱选项，返回 (打乱后的选项, 新位置 -> 原位置)。"""
    order = list(range(len(card["options"])))
    random.Random(f"{SEED}-{card['id']}").shuffle(order)
    return [card["options"][i] for i in order], order


def cmd_export(args: argparse.Namespace) -> int:
    cards, skipped = load_cards()
    random.Random(SEED).shuffle(cards)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in cards:
            options, _ = shuffled(c)
            f.write(json.dumps({"id": c["id"], "chapter": c["chapter"], "stem": c["stem"], "options": options}, ensure_ascii=False) + "\n")
    print(f"导出 {len(cards)} 题到 {args.out}，跳过没有选项的 {skipped} 张")
    return 0


def score(cards: list[dict], answers: dict[str, int]) -> dict:
    by_chapter: dict[tuple[int, str], list[int]] = collections.defaultdict(lambda: [0, 0])
    by_type: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    wrong, missing = [], []
    for c in cards:
        if c["id"] not in answers:
            missing.append(c["id"])
            continue
        _, order = shuffled(c)
        picked = answers[c["id"]] - 1
        ok = 0 <= picked < len(order) and order[picked] in c["correct"]
        for bucket in (by_chapter[(c["chapter"], c["chapter_title"])], by_type[c["type"]]):
            bucket[0] += int(ok)
            bucket[1] += 1
        if not ok:
            wrong.append(c["id"])
    done = sum(v[1] for v in by_chapter.values())
    right = sum(v[0] for v in by_chapter.values())
    return {
        "answered": done,
        "right": right,
        "accuracy": round(right / done * 100, 1) if done else 0.0,
        "by_chapter": {f"{k[0]} {k[1]}": v for k, v in sorted(by_chapter.items())},
        "by_type": dict(by_type),
        "wrong": wrong,
        "missing": missing,
    }


def cmd_score(args: argparse.Namespace) -> int:
    cards, _ = load_cards()
    with open(args.answers, encoding="utf-8-sig", newline="") as f:
        answers = {r["id"]: int(r["answer"]) for r in csv.DictReader(f)}
    result = score(cards, answers)
    print(f"已答 {result['answered']} 题，答对 {result['right']} 题，正确率 {result['accuracy']}%，未答 {len(result['missing'])} 题")
    for k, v in result["by_chapter"].items():
        print(f"  第{k}: {v[0]}/{v[1]} = {v[0] / v[1] * 100:.0f}%")
    for k, v in result["by_type"].items():
        print(f"  {k}: {v[0]}/{v[1]} = {v[0] / v[1] * 100:.0f}%")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def skill_docs() -> str:
    files = [ROOT / "SKILL.md", *ROOT.glob("knowledge/**/*.md"), *ROOT.glob("references/**/*.md")]
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def cmd_coverage(args: argparse.Namespace) -> int:
    """卡片引用的规则名在技能文档里出现过多少。这只说明提到过，不说明讲对了。"""
    docs = skill_docs()
    raw = [json.loads(line) for line in cards_path().open(encoding="utf-8")]
    raw = [c for c in raw if c.get("chapter") != "oracle" and c.get("type") != "oracle"]
    rules: collections.Counter[str] = collections.Counter()
    per_chapter: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for c in raw:
        m = re.search(r"关联规则：\[(.*?)\]", c["text"])
        names = re.findall(r"'([^']+)'", m.group(1)) if m else []
        rules.update(names)
        found = any(n.replace(" ", "") in docs.replace(" ", "") for n in names)
        title = f"{c['chapter']:>2} {c.get('chapter_title', '')}"
        per_chapter[title][0] += int(found)
        per_chapter[title][1] += 1
    missing = [(n, k) for n, k in rules.most_common() if n.replace(" ", "") not in docs.replace(" ", "")]
    total = sum(v[1] for v in per_chapter.values())
    hit = sum(v[0] for v in per_chapter.values())
    print(f"至少一条引用规则能在文档里找到的卡片：{hit}/{total} = {hit / total * 100:.0f}%")
    for k in sorted(per_chapter):
        v = per_chapter[k]
        print(f"  第{k}: {v[0]}/{v[1]}")
    print(f"文档里找不到的规则名（共 {len(missing)} 个，按卡片数排序）：")
    print("  " + "、".join(f"{n}({k})" for n, k in missing[: args.top]))
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export")
    p.add_argument("--out", required=True)
    p = sub.add_parser("score")
    p.add_argument("answers")
    p.add_argument("--json")
    p = sub.add_parser("coverage")
    p.add_argument("--top", type=int, default=60)
    args = parser.parse_args()
    return {"export": cmd_export, "score": cmd_score, "coverage": cmd_coverage}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
