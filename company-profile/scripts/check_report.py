#!/usr/bin/env python3
"""企业画像报告校验与渲染。

用法：
  python check_report.py check  <工作目录>
  python check_report.py render <工作目录> [--force] [--no-quotes]

工作目录中需要：
  evidence.jsonl  证据库，每行一条 JSON（字段见 references/evidence-ledger.md）
  draft.md        报告草稿，用 [E001] 形式引用证据

check  校验证据库和草稿，列出错误（必须修复）和警告（逐条确认）。有错误时退出码为 1。
render 校验无错误后，把 [E001] 按首次出现顺序重编号为 [1]，生成参考来源列表，
       替换 {{来源构成}} 占位符，写出 report.md。

只用标准库。
"""

import json
import re
import sys
from pathlib import Path

TIERS = {"T1", "T2", "T3", "T4"}
ENTITY_STATES = {"confirmed", "related", "rejected"}
REQUIRED_FIELDS = ("id", "url", "title", "publisher", "tier", "date", "entity", "quote")

# 出现在 URL 中即视为 AI 生成内容或 AI 对话分享页，禁止作为证据
BANNED_URL_HINTS = (
    "ai_outputs", "ai-outputs", "aigc", "chatgpt.com/share", "chat.openai.com/share",
    "kimi.moonshot.cn/share", "kimi.com/share", "yiyan.baidu.com/share",
    "doubao.com/thread", "tongyi.aliyun.com/qianwen/share", "chat.deepseek.com/share",
    "metaso.cn/search", "perplexity.ai/search",
)

# 表示推断或综合判断的词，带这些词的无引用句子不报警
INFERENCE_HINTS = ("判断", "推断", "可见", "说明", "意味着", "反映", "表明", "看来", "综合", "见第", "详见",
                   "未检索到", "未披露", "未找到", "未查到", "未见", "未经", "待核实", "已查", "可查", "建议")

# 定性评价用语，必须有出处并写明是谁的评价
EVALUATIVE_WORDS = ("第一梯队", "领先", "龙头", "头部", "标杆", "领军", "首家", "唯一", "最大", "第一",
                    "顶尖", "一流", "知名", "突出", "卓越", "罕见", "护城河", "壁垒")

SUMMARY_SECTIONS = ("核心结论", "冲突")

# note 以这些前缀开头时，在参考来源中标出原文状态
SOURCE_STATUS = (("仅摘要", "摘录为搜索摘要"), ("标题原文", "仅取得标题"))

CITE_RE = re.compile(r"\[(E\d{3,})\]")
CALC_RE = re.compile(r"[（(]\s*计算[：:].*?[）)]")
UNIT_SCALE = {"万亿": 1e12, "亿": 1e8, "千万": 1e7, "百万": 1e6, "万": 1e4, "千": 1e3,
              "trillion": 1e12, "billion": 1e9, "million": 1e6}
NUM_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(万亿|亿|千万|百万|万|千|%|％|trillion|billion|million)?", re.I)

# 先从文本中剔除这些片段，再提取数字：日期、股票代码、字母数字混合的型号名、列表序号
STRIP_PATTERNS = [
    re.compile(r"\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?"),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日"),
    re.compile(r"\d{1,2}\s*月(?:份|末|底|初|中旬|下旬|上旬)?"),
    re.compile(r"\d{4}-\d{1,2}(?:-\d{1,2})?"),
    re.compile(r"\d{4,6}\.(?:HK|SH|SZ|BJ|hk|sh|sz|bj)"),
    re.compile(r"[A-Za-z][\w\-.]*\d[\w\-.]*", re.ASCII),
    re.compile(r"\d[\w\-.]*[A-Za-z][\w\-.]*", re.ASCII),
    re.compile(r"^\s*(?:\d+[.、)]|[-*+])\s+"),
    re.compile(r"第\s*\d+\s*(?:节|章|部分)"),
]


def load_evidence(path):
    errors, warns, ev = [], [], {}
    if not path.exists():
        return ev, [f"找不到证据库 {path}"], warns
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        where = f"evidence.jsonl 第 {n} 行"
        try:
            item = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"{where}：不是合法 JSON（{e.msg}）")
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in item]
        if missing:
            errors.append(f"{where}：缺少字段 {', '.join(missing)}")
            continue
        eid = item["id"]
        if not re.fullmatch(r"E\d{3,}", eid):
            errors.append(f"{where}：id「{eid}」格式应为 E001")
        if eid in ev:
            errors.append(f"{where}：id {eid} 重复")
        if item["tier"] not in TIERS:
            errors.append(f"{where}（{eid}）：tier 必须是 T1–T4")
        if item["entity"] not in ENTITY_STATES:
            errors.append(f"{where}（{eid}）：entity 必须是 confirmed / related / rejected")
        if len(item["quote"].strip()) < 10:
            errors.append(f"{where}（{eid}）：quote 太短，需摘录能支撑事实的原文")
        if not item["url"].strip() and not item.get("locator", "").strip():
            errors.append(f"{where}（{eid}）：url 和 locator 至少填一个")
        url = item["url"].lower()
        for hint in BANNED_URL_HINTS:
            if hint in url:
                errors.append(f"{where}（{eid}）：URL 含「{hint}」，疑似 AI 生成内容，不能作为证据")
                break
        if not item["date"].strip() and not item.get("accessed", "").strip() and item["entity"] != "rejected":
            warns.append(f"{where}（{eid}）：缺少发布日期；持续更新的页面（工商平台、代码仓库）请填 accessed 访问日期")
        ev[eid] = item
    return ev, errors, warns


def to_value(num, unit):
    unit = unit.lower() if unit else unit
    v = float(num.replace(",", ""))
    if unit in ("%", "％"):
        return v, "%"
    return v * UNIT_SCALE.get(unit or "", 1), "abs"


def tolerance(num, unit):
    """按书写精度给出容差：写 0.57 亿，容差为 0.005 亿。"""
    s = num.replace(",", "")
    decimals = len(s.split(".")[1]) if "." in s else 0
    step = 0.5 * 10 ** (-decimals)
    unit = unit.lower() if unit else unit
    if unit in ("%", "％"):
        return step
    return step * UNIT_SCALE.get(unit or "", 1)


def extract_numbers(text):
    text = CITE_RE.sub(" ", text)
    for p in STRIP_PATTERNS:
        text = p.sub(" ", text)
    out = []
    for m in NUM_RE.finditer(text):
        num, unit = m.group(1), m.group(2)
        if not re.search(r"\d", num):
            continue
        out.append((m.group(0).strip(), num, unit))
    return out


def number_supported(num, unit, quotes):
    val, kind = to_value(num, unit)
    tol = tolerance(num, unit)
    for q in quotes:
        for _, qn, qu in extract_numbers(q):
            qv, qk = to_value(qn, qu)
            if qk == kind and abs(qv - val) <= tol + 1e-9 * max(1, abs(val)):
                return True
    return False


def split_draft(text):
    """把草稿切成 (章节, 行号, 片段) 列表。表格的每一行是一个片段；表格下方
    以「来源：」开头的行里的引用，由该表格每一行继承。"""
    lines = text.splitlines()
    segs = []
    section = "（文首）"
    in_header = True
    i = 0
    while i < len(lines):
        line = lines[i]
        lineno = i + 1
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            in_header = not line.startswith("##")
            i += 1
            continue
        # 一级标题下、第一个二级标题前的引用块是报告信息栏，不校验
        if (in_header and line.lstrip().startswith(">")) or line.strip().startswith("<!--") or not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append((i + 1, lines[i]))
                i += 1
            inherited = ""
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and re.match(r"\s*(来源|资料来源|数据来源)[：:]", lines[j]):
                inherited = " ".join(CITE_RE.findall(lines[j]))
                inherited = " ".join(f"[{x}]" for x in inherited.split())
            for k, (ln, row) in enumerate(rows):
                if k == 0 or re.fullmatch(r"\s*\|[\s:\-|]+\|\s*", row):
                    continue  # 表头和分隔行
                segs.append((section, ln, row + " " + inherited, True))
            continue
        # 普通段落：按句末标点切句，句末标点后紧跟的引用归前一句
        parts = re.split(r"(?<=[。！？；])", line)
        # 句号落在加粗引导语里（**结论。**）时，引导语和后一句合并
        joined = []
        for p in parts:
            if joined and joined[-1].count("**") % 2 == 1:
                joined[-1] += p
            else:
                joined.append(p)
        parts = joined
        merged = []
        for p in parts:
            lead = re.match(r"^\s*((?:\[E\d{3,}\])+)", p)
            if lead and merged:
                merged[-1] += lead.group(1)
                p = p[lead.end():]
            if p.strip():
                merged.append(p)
        for p in merged:
            segs.append((section, lineno, p, False))
        i += 1
    return segs


def check(workdir):
    ev, errors, warns = load_evidence(workdir / "evidence.jsonl")
    draft_path = workdir / "draft.md"
    if not draft_path.exists():
        errors.append(f"找不到草稿 {draft_path}")
        return ev, errors, warns, []
    text = draft_path.read_text(encoding="utf-8")
    if re.search(r"^#+\s*参考来源", text, re.M):
        errors.append("draft.md 不要自己写参考来源，render 会根据证据库生成")
    if re.search(r"\[\d+\]", text):
        errors.append("draft.md 里出现了 [1] 这样的数字编号；草稿只用 [E001] 形式，编号由 render 生成")

    infos = []
    value_sections = {}
    cited_ids = set()
    for section, ln, seg, is_row in split_draft(text):
        where = f"draft.md 第 {ln} 行（{section}）"
        ids = list(dict.fromkeys(CITE_RE.findall(seg)))
        cited_ids.update(ids)
        unknown = [x for x in ids if x not in ev]
        for x in unknown:
            errors.append(f"{where}：引用的 {x} 不在证据库中")
        known = [ev[x] for x in ids if x in ev]
        for item in known:
            if item["entity"] == "rejected":
                errors.append(f"{where}：引用了已判定为非本主体的证据 {item['id']}")
            elif item["entity"] == "related":
                warns.append(f"{where}：{item['id']} 属于关联主体，确认句中写明了是哪家主体")

        body = CITE_RE.sub("", seg)
        has_calc = bool(CALC_RE.search(body))
        nums = extract_numbers(CALC_RE.sub(" ", body))
        snippet = body.strip()[:40]
        if nums and not ids:
            errors.append(f"{where}：含数字但没有引用 →「{snippet}」")
            continue
        if not ids and not is_row:
            plain = re.sub(r"[\s*_>`|]", "", body)
            hits = [w for w in EVALUATIVE_WORDS if w in plain]
            if hits:
                errors.append(f"{where}：评价性措辞「{'、'.join(hits)}」没有出处；补引用并写明是谁的评价，或删掉 →「{snippet}」")
                continue
        if not nums and not ids and not is_row:
            plain = re.sub(r"[\s*_>`|]", "", body)
            if len(plain) > 15 and not any(h in plain for h in INFERENCE_HINTS):
                warns.append(f"{where}：陈述没有引用，若是事实请补引用，若是判断请写明依据 →「{snippet}」")
            continue
        quotes = [it["quote"] for it in known]
        for raw, num, unit in nums:
            key = (round(to_value(num, unit)[0], 6), to_value(num, unit)[1])
            value_sections.setdefault(key, {}).setdefault(section, raw)
            if known and not number_supported(num, unit, quotes):
                msg = f"{where}：数字「{raw}」在所引证据原文中找不到"
                if has_calc:
                    infos.append(msg + "（句中有计算说明，请确认计算过程）")
                else:
                    errors.append(msg + "；改成原文数字、换引用，或加「（计算：…）」说明换算")
        for calc in CALC_RE.findall(body):
            for raw, num, unit in extract_numbers(calc):
                if known and not number_supported(num, unit, quotes):
                    errors.append(f"{where}：计算说明里的输入数字「{raw}」在所引证据原文中找不到")
        if nums and known and all(it["tier"] == "T4" for it in known) and "待核实" not in body:
            warns.append(f"{where}：关键数字只有 T4 来源支撑，需找 T1–T3 来源或标注【待核实】")

    for key, secs in value_sections.items():
        # 核心结论是摘要，冲突一节需要并列各方数字，都不计入重复
        secs = {k: v for k, v in secs.items() if not any(w in k for w in SUMMARY_SECTIONS)}
        if len(secs) >= 3 and key[0] >= 10:
            sample = next(iter(secs.values()))
            warns.append(f"数字「{sample}」出现在 {len(secs)} 个章节（{'、'.join(secs)}），检查是否跨章节重复叙述")

    uncited_t4 = sum(1 for x in cited_ids if x in ev and ev[x]["tier"] == "T4")
    if cited_ids:
        share = uncited_t4 / len(cited_ids)
        if share > 0.4:
            warns.append(f"被引用证据中 T4 占 {share:.0%}，来源层级偏低")
        if not any(ev[x]["tier"] == "T1" for x in cited_ids if x in ev):
            warns.append("没有引用任何 T1 来源（法定披露、工商登记、官方文件）")
    return ev, errors, warns, infos


def print_report(errors, warns, infos):
    for title, items in (("错误（必须修复）", errors), ("警告（逐条确认）", warns), ("提示", infos)):
        if items:
            print(f"\n== {title}：{len(items)} 条")
            for it in items:
                print(f"  - {it}")
    if not (errors or warns or infos):
        print("校验通过，没有问题。")
    else:
        print(f"\n合计：错误 {len(errors)}，警告 {len(warns)}，提示 {len(infos)}")


def render(workdir, ev, with_quotes):
    text = (workdir / "draft.md").read_text(encoding="utf-8")
    order = []
    for eid in CITE_RE.findall(text):
        if eid not in order:
            order.append(eid)
    num = {eid: i + 1 for i, eid in enumerate(order)}
    out = CITE_RE.sub(lambda m: f"[{num[m.group(1)]}]", text)
    out = re.sub(r"(\[\d+\])(\1)+", r"\1", out)

    counts = {t: sum(1 for e in order if ev[e]["tier"] == t) for t in sorted(TIERS)}
    stats = f"共 {len(order)} 条：" + "、".join(f"{t} {c} 条" for t, c in counts.items())
    out = out.replace("{{来源构成}}", stats)

    refs = ["", "## 参考来源", "",
            "> 来源等级：T1 法定披露与官方登记；T2 政府、监管、权威媒体与机构；"
            "T3 行业媒体与研究机构；T4 自媒体、百科、论坛等。", ""]
    for eid in order:
        it = ev[eid]
        date = f"，{it['date']}" if it["date"] else (f"，访问于 {it['accessed']}" if it.get("accessed") else "")
        loc = f"，{it['locator']}" if it.get("locator") else ""
        note = it.get("note", "")
        status = next((f"，{tag}" for key, tag in SOURCE_STATUS if note.startswith(key)), "")
        line = f"[{num[eid]}] {it['publisher']}：{it['title']}（{it['tier']}{date}{loc}{status}）{it['url']}"
        if with_quotes:
            q = it["quote"].strip().replace("\n", " ")
            q = q if len(q) <= 90 else q[:90] + "…"
            label = "摘要" if note.startswith("仅摘要") else "原文摘录"
            line += f"  \n    {label}：「{q}」"
        refs.append(line)
    (workdir / "report.md").write_text(out.rstrip() + "\n" + "\n".join(refs) + "\n", encoding="utf-8")
    print(f"已生成 {workdir / 'report.md'}（{stats}）")


def main(argv):
    if len(argv) < 3 or argv[1] not in ("check", "render"):
        print(__doc__)
        return 2
    workdir = Path(argv[2])
    ev, errors, warns, infos = check(workdir)
    print_report(errors, warns, infos)
    if argv[1] == "render":
        if errors and "--force" not in argv:
            print("\n存在错误，未生成 report.md。修复后重试。")
            return 1
        render(workdir, ev, "--no-quotes" not in argv)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
