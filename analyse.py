# -*- coding: utf-8 -*-
"""
evaluate.py — 学生画像 · 离线质量评估（批量处理所有 run_* 目录）

一键批量评估（扫描 ./output 下的所有 run_*，逐一评估并汇总）：
    python evaluate.py

只评估某个 run 目录：
    python evaluate.py --input_dir ./output/run_XXXXXX

可选参数：
    --simhash_threshold 3    # 近似检测阈值（汉明距离 <= 阈值 视为相似）
    --topk 100               # 近似对最多保留多少条
    --out_root ./eval        # 评估结果根目录（默认 ./eval）

本版修订要点：
- 修正“代理名”判定：支持复姓与连写名、以及名内/名间用下划线分节的多种合法形式
  示例（皆合法）：li1_huan4ying1 / li1_huan4_ying1 / yao2_zi3qing2 / wang2_xiao3_ming2 / ou3yang2_xin1yi2
"""

import os, re, json, glob, argparse, hashlib, csv
from collections import Counter, defaultdict
from typing import List, Dict, Any, Tuple

# -------------------- 常量 & 规则 --------------------
STRICT_ALLOWED_STRINGS = {
    "高：成绩全校排名前10%",
    "中：成绩全校排名前10%至30%",
    "低：成绩全校排名前30%至50%",
    "差：成绩全校排名后50%"
}

REQUIRED_KEYS = ["id","姓名","年龄","擅长科目","薄弱科目","年级","人格","社交关系",
                 "学术水平","性别","发展阶段","代理名","价值观","创造力","心理健康"]

PARAGRAPH_FIELDS = ["价值观","创造力","心理健康"]
VAL_DIMS7 = ["道德修养","身心健康","法治意识","社会责任","政治认同","文化素养","家庭观念"]
VAL_LVLS = ["高","较高","中","中上","较低","低"]
CRE_DIMS8 = ["流畅性","新颖性","灵活性","可行性","问题发现","问题分析","提出方案","改善方案"]
PSY_KEYS  = ["综合心理状况","幸福指数","抑郁风险","焦虑风险","信息不足或未见显著症状","背景","应对","支持","家庭","同伴","老师","转折"]

DEFAULT_OUTPUT_ROOT = os.path.join(os.getcwd(), "output")
DEFAULT_EVAL_ROOT   = os.path.join(os.getcwd(), "eval")

# -------------------- I/O --------------------
def read_all_records(input_dir: str) -> List[Dict[str, Any]]:
    files = sorted(glob.glob(os.path.join(input_dir, "students_chunk_*.jsonl")))
    out = []
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                    out.append(obj)
                except:
                    pass
    return out

def count_records_in_dir(input_dir: str) -> int:
    files = glob.glob(os.path.join(input_dir, "students_chunk_*.jsonl"))
    total = 0
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    total += 1
    return total

# -------------------- 文本与工具 --------------------
def non_empty(v: Any) -> bool:
    if v is None: return False
    if isinstance(v, str): return v.strip() != ""
    if isinstance(v, (list, dict)): return len(v) > 0
    return True

def is_single_paragraph(s: str) -> bool:
    if not isinstance(s, str): return False
    # 禁止双换行或明显的项目符号
    if re.search(r"(\n\s*\n)|(^\s*[-•\d]+\.)", s): return False
    return True

def text_to_ngrams(t: str, n: int = 3):
    t = re.sub(r"\s+", "", str(t))
    return [t[i:i+n] for i in range(max(0, len(t)-n+1))] if t else []

def simhash64(text: str) -> int:
    bits = 64
    v = [0]*bits
    for g in text_to_ngrams(text, 3):
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if ((h >> i) & 1) else -1
    out = 0
    for i in range(bits):
        if v[i] >= 0:
            out |= (1 << i)
    return out

def hamming(a: int, b: int) -> int:
    x = a ^ b
    cnt = 0
    while x:
        x &= x-1
        cnt += 1
    return cnt

def distinct_n(texts: List[str], n: int = 1) -> float:
    tokens = []
    for t in texts:
        t = re.sub(r"\s+", " ", str(t)).strip()
        tokens.extend(t.split())
    if len(tokens) < n: return 0.0
    ngrams = set(tuple(tokens[i:i+n]) for i in range(len(tokens)-n+1))
    denom = max(1, len(tokens)-n+1)
    return len(ngrams) / denom

def build_key_text(record: Dict[str, Any]) -> str:
    return "｜".join([
        str(record.get("年级","")), str(record.get("性别","")),
        " ".join(record.get("人格",[]) if isinstance(record.get("人格"), list) else [str(record.get("人格",""))]),
        str(record.get("价值观","")), str(record.get("社交关系","")),
        str(record.get("创造力","")), str(record.get("心理健康",""))
    ])

# -------------------- 代理名：稳健校验（支持复姓与名的多音节连写） --------------------
_SYL = re.compile(r"^[a-z]+[1-5]$")  # 单音节：小写字母+1位声调

def _count_syllables_concat(token: str) -> int:
    """
    统计 token 内按“音节+声调”连写的音节数（每个音节形如 [a-z]+[1-5]）。
    例如：'huan4ying1' -> 2；'li3' -> 1；不合法返回 0。
    """
    if not isinstance(token, str) or not token:
        return 0
    i, n, cnt = 0, len(token), 0
    while i < n:
        # 连续字母段
        j = i
        while j < n and 'a' <= token[j] <= 'z':
            j += 1
        # 必须跟一个 1-5 的声调数字
        if j == i or j >= n or token[j] not in "12345":
            return 0
        cnt += 1
        i = j + 1
    return cnt

def is_valid_agent_name(name: str) -> bool:
    """
    允许格式（全小写）：
      - <姓(1-2 音节连写)> _ <名(1-2 音节连写)>
      - <姓(1 音节)> _ <名各音节用下划线分开（每节 1 音节）>
      - 也允许全名每个音节都用下划线分开（>=2 节）
    约束：
      - 每个音节形如 [a-z]+[1-5]（声调必填）
      - 下划线至少 1 个（分隔姓与名）；不得出现空段
    """
    if not isinstance(name, str) or "_" not in name:
        return False
    parts = name.split("_")
    if any(p == "" for p in parts):
        return False

    # 情况 A：所有 part 都是单音节（如 wang2_xiao3_ming2）
    if all(_SYL.match(p) for p in parts):
        return len(parts) >= 2  # 至少 姓+名

    # 情况 B：允许连写：首段(姓)为 1-2 音节连写；其余各段(名的片段)为 1-2 音节连写
    syl_first = _count_syllables_concat(parts[0])
    if syl_first not in (1, 2):
        return False
    for p in parts[1:]:
        syl = _count_syllables_concat(p)
        if syl == 0 or syl > 2:
            return False
    return True

# -------------------- 规则检查 --------------------
def check_required(record: Dict[str, Any]) -> List[str]:
    miss = []
    for k in REQUIRED_KEYS:
        if k not in record or not non_empty(record[k]):
            miss.append(f"缺失或为空：{k}")
    return miss

def check_academic(record: Dict[str, Any]) -> List[str]:
    issues = []
    lvl = record.get("学术水平","")
    if lvl not in STRICT_ALLOWED_STRINGS:
        issues.append("学术水平非四选一固定文案")
    good = record.get("擅长科目",[])
    weak = record.get("薄弱科目",[])
    if not isinstance(good, list) or not good:
        issues.append("擅长科目为空或非数组")
    if not isinstance(weak, list) or not weak:
        issues.append("薄弱科目为空或非数组")
    if isinstance(good, list) and isinstance(weak, list):
        if set(good) & set(weak):
            issues.append("擅长/薄弱科目集合相交")
    return issues

def check_agent_name(record: Dict[str, Any]) -> List[str]:
    issues = []
    name = str(record.get("代理名",""))
    if not is_valid_agent_name(name):
        issues.append("代理名不合规（小写拼音+声调；姓/名用下划线分隔；复姓/名可1–2音节连写或分节）")
    return issues

def check_paragraphs(record: Dict[str, Any]) -> List[str]:
    issues = []
    for f in PARAGRAPH_FIELDS:
        if not is_single_paragraph(record.get(f,"")):
            issues.append(f"{f} 非单段体裁")
    return issues

def check_value_dims(record: Dict[str, Any]) -> List[str]:
    issues = []
    val = str(record.get("价值观",""))
    if not any(dim in val for dim in VAL_DIMS7):
        issues.append("价值观未见七维显式名词（至少缺大部分）")
    if not any(lv in val for lv in VAL_LVLS):
        issues.append("价值观未见等级词")
    return issues

def check_creativity_dims(record: Dict[str, Any]) -> List[str]:
    issues = []
    cre = str(record.get("创造力",""))
    if not any(dim in cre for dim in CRE_DIMS8):
        issues.append("创造力未见八维显式名词（至少缺大部分）")
    if ("可行性较低" in cre or "可行性低" in cre) and ("提出方案高" in cre or "提出方案较高" in cre):
        issues.append("创造力一致性：可行性低但‘提出方案’高")
    if not ("雷达" in cre or "总结" in cre):
        issues.append("创造力未见雷达总结提示词")
    return issues

def check_psychology_slots(record: Dict[str, Any]) -> List[str]:
    issues = []
    psy = str(record.get("心理健康",""))
    if not any(k in psy for k in PSY_KEYS):
        issues.append("心理健康未见关键槽位关键词")
    if "身心健康高" in str(record.get("价值观","")) or "身心健康较高" in str(record.get("价值观","")):
        if "重度" in psy:
            issues.append("一致性：价值观身心健康高/较高 ↔ 心理出现重度术语")
    return issues

def check_age_grade_consistency(record: Dict[str, Any]) -> List[str]:
    age = record.get("年龄")
    grade = str(record.get("年级",""))
    issues = []
    if not isinstance(age, int):
        return ["年龄非整数或缺失"]
    grade_map = {
        "五年级": (10,11), "六年级": (11,12),
        "初一": (12,13), "初二": (13,14), "初三": (14,15),
        "高一": (15,16), "高二": (16,17), "高三": (17,18)
    }
    if grade in grade_map:
        lo, hi = grade_map[grade]
        if not (lo-1 <= age <= hi+1):
            issues.append(f"年龄-年级非常模(年级:{grade}, 年龄:{age})")
    return issues

# -------------------- 单目录评估 --------------------
def evaluate(records: List[Dict[str, Any]], simhash_th: int = 3, topk: int = 100) -> Dict[str, Any]:
    per_item_results = []
    simhashes = []
    texts = []

    for idx, rec in enumerate(records):
        errs, warns = [], []
        errs += check_required(rec)
        errs += check_academic(rec)
        errs += check_agent_name(rec)
        errs += check_paragraphs(rec)

        warns += check_value_dims(rec)
        warns += check_creativity_dims(rec)
        warns += check_psychology_slots(rec)
        warns += check_age_grade_consistency(rec)

        per_item_results.append({
            "id": rec.get("id", idx+1),
            "姓名": rec.get("姓名",""),
            "年级": rec.get("年级",""),
            "性别": rec.get("性别",""),
            "学术水平": rec.get("学术水平",""),
            "errors": errs,
            "warnings": warns
        })
        key_text = build_key_text(rec)
        texts.append(key_text)
        simhashes.append(simhash64(key_text))

    # 近似对（粗分桶，再精对比）
    near_pairs = []
    buckets = defaultdict(list)
    for i, h in enumerate(simhashes):
        bucket_key = h >> 48  # 高16位分桶
        buckets[bucket_key].append(i)
    for idxs in buckets.values():
        m = len(idxs)
        for i in range(m):
            for j in range(i+1, m):
                a, c = idxs[i], idxs[j]
                if hamming(simhashes[a], simhashes[c]) <= simhash_th:
                    near_pairs.append({
                        "i": a, "j": c,
                        "id_i": per_item_results[a]["id"],
                        "id_j": per_item_results[c]["id"],
                        "姓名_i": per_item_results[a]["姓名"],
                        "姓名_j": per_item_results[c]["姓名"]
                    })
    near_pairs = near_pairs[:topk]

    # Distinct-1/2
    d1 = distinct_n(texts, 1)
    d2 = distinct_n(texts, 2)

    # 覆盖度分布
    grade_cnt = Counter([str(r.get("年级","")) for r in records])
    gender_cnt = Counter([str(r.get("性别","")) for r in records])
    level_cnt  = Counter([str(r.get("学术水平","")) for r in records])

    # 错误/警告率
    err_rate = sum(1 for x in per_item_results if len(x["errors"])>0) / max(1, len(per_item_results))
    warn_rate = sum(1 for x in per_item_results if len(x["warnings"])>0) / max(1, len(per_item_results))

    # 简易评分（0-100）
    n_rec = max(1, len(records))
    miss_rate = sum(1 for r in per_item_results if any("缺失或为空" in e for e in r["errors"])) / n_rec
    para_bad  = sum(1 for r in per_item_results if any("非单段体裁" in e for e in r["errors"])) / n_rec
    age_bad   = sum(1 for r in per_item_results if any("年龄-年级非常模" in w or "年龄非整数或缺失" in w for w in r["warnings"])) / n_rec
    lvl_bad   = sum(1 for r in per_item_results if any("学术水平非四选一" in e for e in r["errors"])) / n_rec
    div_score = max(0.0, min(1.0, 0.5*(d1/0.4) + 0.5*(d2/0.6)))

    score = (
        (1-miss_rate)*25 +
        (1-age_bad)*25 +
        (1-para_bad)*15 +
        div_score*20 +
        (1-lvl_bad)*15
    )

    summary = {
        "count": len(records),
        "error_rate": round(err_rate, 4),
        "warning_rate": round(warn_rate, 4),
        "distinct_1": round(d1, 4),
        "distinct_2": round(d2, 4),
        "near_duplicate_pairs": len(near_pairs),
        "score_overall_0_100": round(score, 2),
        "distributions": {
            "grade": dict(grade_cnt),
            "gender": dict(gender_cnt),
            "academic_level": dict(level_cnt)
        }
    }

    suspicious = sorted(
        per_item_results,
        key=lambda x: (len(x["errors"]), len(x["warnings"])),
        reverse=True
    )[:min(100, len(per_item_results))]

    return {
        "summary": summary,
        "per_item": per_item_results,
        "near_pairs": near_pairs,
        "suspicious": suspicious
    }

# -------------------- 保存 --------------------
def save_one_run(outdir: str, result: Dict[str,Any], run_id: str):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(result["summary"], f, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, "per_item.jsonl"), "w", encoding="utf-8") as f:
        for r in result["per_item"]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(outdir, "near_duplicates.json"), "w", encoding="utf-8") as f:
        json.dump(result["near_pairs"], f, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, "suspicious_samples.json"), "w", encoding="utf-8") as f:
        json.dump(result["suspicious"], f, ensure_ascii=False, indent=2)

    # buckets.csv
    rows = [["bucket","key","count"]]
    dist = result["summary"]["distributions"]
    for k,v in dist["grade"].items():          rows.append(["grade", k, v])
    for k,v in dist["gender"].items():         rows.append(["gender", k, v])
    for k,v in dist["academic_level"].items(): rows.append(["academic_level", k, v])
    with open(os.path.join(outdir, "buckets.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)

    # dashboard.md
    s = result["summary"]
    md = []
    md.append(f"# 学生画像 · 离线评估看板（{run_id}）\n")
    md.append(f"- 总数: **{s['count']}**")
    md.append(f"- 错误率: **{s['error_rate']*100:.2f}%**；警告率: **{s['warning_rate']*100:.2f}%**")
    md.append(f"- 多样性: Distinct-1 **{s['distinct_1']}** / Distinct-2 **{s['distinct_2']}**")
    md.append(f"- 近似文本对(SimHash≤阈): **{s['near_duplicate_pairs']}**")
    md.append(f"- 综合评分(0-100): **{s['score_overall_0_100']}**\n")
    md.append("## 覆盖度分布\n")
    for bucket, label in [("grade","年级"),("gender","性别"),("academic_level","学术水平")]:
        md.append(f"**{label}**: " + ", ".join([f"{k}:{v}" for k,v in dist[bucket].items()]) + "  ")
    md.append("\n## Top 可疑样本（部分）\n")
    for r in result["suspicious"][:20]:
        md.append(f"- id={r['id']} 姓名={r['姓名']} 年级={r['年级']} 性别={r['性别']}  "
                  f"Errors={len(r['errors'])} Warnings={len(r['warnings'])}")
    with open(os.path.join(outdir, "dashboard.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

# -------------------- 运行目录发现 --------------------
def list_run_dirs(output_root: str) -> List[str]:
    if not os.path.isdir(output_root):
        return []
    candidates = [d for d in os.listdir(output_root)
                  if d.startswith("run_") and os.path.isdir(os.path.join(output_root, d))]
    # 按 run_时间戳倒序
    def run_key(x: str):
        try:
            return int(x.split("_",1)[1])
        except:
            return -1
    candidates.sort(key=run_key, reverse=True)
    return [os.path.join(output_root, d) for d in candidates]

# -------------------- 批量主流程 --------------------
def evaluate_one_run(run_dir: str, out_root: str, simhash_th: int, topk: int) -> Dict[str, Any]:
    run_id = os.path.basename(os.path.normpath(run_dir))
    records = read_all_records(run_dir)
    if not records:
        print(f"[skip] {run_id}: 无有效记录，跳过。")
        return {}
    print(f"[run]  {run_id}: 读取 {len(records)} 条，开始评估…")
    result = evaluate(records, simhash_th=simhash_th, topk=topk)
    outdir = os.path.join(out_root, run_id)
    save_one_run(outdir, result, run_id)
    # 返回简明汇总用于全局索引
    s = result["summary"]
    return {
        "run_id": run_id,
        "count": s["count"],
        "error_rate": s["error_rate"],
        "warning_rate": s["warning_rate"],
        "distinct_1": s["distinct_1"],
        "distinct_2": s["distinct_2"],
        "near_duplicate_pairs": s["near_duplicate_pairs"],
        "score_overall_0_100": s["score_overall_0_100"]
    }

def evaluate_all_runs(output_root: str, out_root: str, simhash_th: int, topk: int) -> None:
    os.makedirs(out_root, exist_ok=True)
    run_dirs = list_run_dirs(output_root)
    if not run_dirs:
        raise SystemExit(f"未在 {output_root} 下找到任何 run_* 目录。")

    index_rows = [["run_id","count","error_rate","warning_rate","distinct_1","distinct_2","near_duplicate_pairs","score_overall_0_100"]]
    index_json = []
    processed = 0

    for rd in run_dirs:
        summ = evaluate_one_run(rd, out_root, simhash_th, topk)
        if not summ:
            continue
        processed += 1
        index_rows.append([
            summ["run_id"], summ["count"], summ["error_rate"], summ["warning_rate"],
            summ["distinct_1"], summ["distinct_2"], summ["near_duplicate_pairs"], summ["score_overall_0_100"]
        ])
        index_json.append(summ)

    if processed == 0:
        raise SystemExit(f"在 {output_root} 下的 run_* 目录均无有效 jsonl 数据，无法评估。")

    # 保存全局索引
    with open(os.path.join(out_root, "_index.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(index_rows)
    with open(os.path.join(out_root, "_index.json"), "w", encoding="utf-8") as f:
        json.dump(index_json, f, ensure_ascii=False, indent=2)

    # 生成 overview.md
    md = ["# 离线评估总览（所有 run_*）\n"]
    md.append(f"共评估 **{processed}** 个 run 目录，结果存放于各自的子目录（如 `eval/run_xxx/`）。\n")
    md.append("| run_id | count | error_rate | warning_rate | distinct_1 | distinct_2 | near_dups | score |\n")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for r in index_json:
        md.append(f"| {r['run_id']} | {r['count']} | {r['error_rate']:.4f} | {r['warning_rate']:.4f} | "
                  f"{r['distinct_1']:.4f} | {r['distinct_2']:.4f} | {r['near_duplicate_pairs']} | {r['score_overall_0_100']:.2f} |")
    with open(os.path.join(out_root, "overview.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"[OK] 已完成批量评估：{processed} 个 run。总览索引：{os.path.join(out_root, 'overview.md')}")

# -------------------- CLI --------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", default=None, help="仅评估该目录（形如 ./output/run_xxx）。不传则批量评估 ./output 下所有 run_*。")
    ap.add_argument("--out_root", default=DEFAULT_EVAL_ROOT, help="评估结果根目录（默认 ./eval）")
    ap.add_argument("--simhash_threshold", type=int, default=3)
    ap.add_argument("--topk", type=int, default=100)
    args = ap.parse_args()

    output_root = DEFAULT_OUTPUT_ROOT
    out_root = args.out_root
    sim_th = args.simhash_threshold
    topk = args.topk

    if args.input_dir:
        # 单 run 评估
        run_dir = os.path.abspath(args.input_dir)
        if not os.path.isdir(run_dir):
            raise SystemExit(f"指定目录不存在：{run_dir}")
        summ = evaluate_one_run(run_dir, out_root, sim_th, topk)
        if not summ:
            raise SystemExit(f"{run_dir} 下没有有效数据（或全为空），无法评估。")
        print(f"[OK] 单目录评估完成：{run_dir} -> {os.path.join(out_root, os.path.basename(run_dir))}")
    else:
        # 批量评估所有 run_*（自动跳过空目录，不退出）
        evaluate_all_runs(output_root, out_root, sim_th, topk)

if __name__ == "__main__":
    main()
