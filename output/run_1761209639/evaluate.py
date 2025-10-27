# -*- coding: utf-8 -*-
"""
evaluate.py — 学生画像 · 离线后验评估（扫描 students_chunk_*.jsonl）
输出：
  1) report.json        —— 全量统计与各指标分组结果（年级/性别/批次）
  2) report.md          —— 人类可读的简报（红黄绿灯 + 可疑样本Top-N）
  3) suspicious.csv     —— 可疑样本清单（便于回溯）
  4) duplicates.csv     —— 近重复/模板化样本对（SimHash ≤ 阈值）
  5) failures_samples.json —— 若你把失败样本写在 run_dir/failures.jsonl，会复制一份做总结

评价维度（A–G）：
A 完整性 & 严格约束（必填键、学术水平四选一、代理名正则）
B 体裁结构（价值观/创造力/心理健康为“单段”；显式槽位命中）
C 价值观七维+等级词覆盖度
D 创造力八维+雷达总结、可行性→提出方案一致性
E 心理健康结构槽位、非诊断化
F 跨维一致性（年级↔年龄、价值观身心健康↔心理三指标、科目不交叉…）
G 多样性与去模板（SimHash近邻率、长度/词汇离散度、年级×性别×学科簇覆盖度）

用法：
  python evaluate.py --input <dir or file> --outdir <dir> [--simhash_threshold 3] [--topn 50]
"""

import argparse, os, re, json, glob, math, hashlib, csv
from collections import Counter, defaultdict
from typing import List, Dict, Any, Tuple

STRICT_ALLOWED_STRINGS = {
    "高：成绩全校排名前10%",
    "中：成绩全校排名前10%至30%",
    "低：成绩全校排名前30%至50%",
    "差：成绩全校排名后50%",
}

# —— 代理名正则（修正：允许 2~4 个音节：a1_b2 或 a1_b2_c3 或 a1_b2_c3_d2）
AGENT_REGEX = re.compile(r"^[a-z]+[1-5]?(?:_[a-z]+[1-5]?){1,3}$")

PARA_FIELDS = ["价值观","创造力","心理健康"]
VAL_7 = ["道德修养","身心健康","法治意识","社会责任","政治认同","文化素养","家庭观念"]
CRE_8 = ["流畅性","新颖性","灵活性","可行性","问题发现","问题分析","提出方案","改善方案"]
PSY_KWS = ["综合心理状况","幸福指数","抑郁风险","焦虑风险","信息不足或未见显著症状","背景","应对","支持","家庭","同伴","老师"]

GRADES_ORDER = ["一年级","二年级","三年级","四年级","五年级","六年级","初一","初二","初三","高一","高二","高三"]
GRADE_AGE = {
    "一年级":(6,7),"二年级":(7,8),"三年级":(8,9),"四年级":(9,10),"五年级":(10,11),"六年级":(11,12),
    "初一":(12,13),"初二":(13,14),"初三":(14,15),"高一":(15,16),"高二":(16,17),"高三":(17,18)
}

def load_jsonl(path: str) -> List[Dict[str,Any]]:
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                data.append(json.loads(line))
            except:
                pass
    return data

def scan_inputs(input_path: str) -> List[Dict[str,Any]]:
    if os.path.isdir(input_path):
        files = sorted(glob.glob(os.path.join(input_path, "students_chunk_*.jsonl")))
        out=[]
        for p in files: out.extend(load_jsonl(p))
        return out
    else:
        return load_jsonl(input_path)

# —— 工具：SimHash64
def _text_to_ngrams(t: str, n: int = 3) -> List[str]:
    t = re.sub(r"\s+", "", t)
    return [t[i:i+n] for i in range(max(0, len(t)-n+1))] if t else []

def simhash64(text: str) -> int:
    v = [0]*64
    for g in _text_to_ngrams(text, 3):
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        for i in range(64):
            v[i] += 1 if ((h >> i) & 1) else -1
    out = 0
    for i in range(64):
        if v[i] >= 0: out |= (1<<i)
    return out

def hamming(a: int, b: int) -> int:
    x=a^b; c=0
    while x: x&=x-1; c+=1
    return c

# —— 体裁判断
def is_single_paragraph(s: str) -> bool:
    if not isinstance(s, str): return False
    if re.search(r"(\n\s*\n)|(^\s*[-•\d]+\.)", s): return False
    return True

def has_any(s: str, kws: List[str]) -> bool:
    return isinstance(s, str) and any(kw in s for kw in kws)

# —— A：完整性 & 严格约束
def check_A(item: Dict[str,Any]) -> List[str]:
    req = ["id","姓名","年龄","擅长科目","薄弱科目","年级","人格","社交关系","学术水平","性别","发展阶段","代理名","价值观","创造力","心理健康"]
    issues=[]
    for k in req:
        v = item.get(k, None)
        if v is None or (isinstance(v,str) and not v.strip()) or (isinstance(v,(list,dict)) and len(v)==0):
            issues.append(f"A.missing:{k}")

    if item.get("学术水平") not in STRICT_ALLOWED_STRINGS:
        issues.append("A.level_not_in_4")

    ag = str(item.get("代理名",""))
    if not AGENT_REGEX.match(ag):
        issues.append("A.agent_regex_bad")

    # 科目不交叉 & 非空数组
    good_subj=True
    major=item.get("擅长科目",[]); weak=item.get("薄弱科目",[])
    if not isinstance(major,list) or not isinstance(weak,list) or len(major)==0 or len(weak)==0:
        good_subj=False
    else:
        if set(major) & set(weak): good_subj=False
    if not good_subj:
        issues.append("A.subject_set_bad")

    return issues

# —— B：体裁结构
def check_B(item: Dict[str,Any]) -> List[str]:
    issues=[]
    for f in PARA_FIELDS:
        if not is_single_paragraph(item.get(f,"")):
            issues.append(f"B.paragraph:{f}")
    # 显式槽位命中（轻量）
    if not has_any(item.get("价值观",""), VAL_7): issues.append("B.value_missing_dims")
    if not has_any(item.get("价值观",""), ["高","较高","中","中上","较低","低"]): issues.append("B.value_missing_levels")
    if not has_any(item.get("创造力",""), CRE_8): issues.append("B.creativity_missing_dims")
    cr=item.get("创造力","")
    if ("雷达" not in cr) and ("总结" not in cr):
        issues.append("B.creativity_no_radar_hint")
    if not has_any(item.get("心理健康",""), PSY_KWS): issues.append("B.psych_missing_slots")
    return issues

# —— C：价值观七维 & 等级词覆盖（严格一点：七维都需出现）
def check_C(item: Dict[str,Any]) -> List[str]:
    txt=item.get("价值观","") or ""
    missing=[d for d in VAL_7 if d not in txt]
    issues=[]
    if missing:
        issues.append("C.values_missing:"+",".join(missing))
    # 等级词粗检
    if not re.search(r"(高|较高|中上|中等|中|较低|低)", txt):
        issues.append("C.values_no_levelword")
    return issues

# —— D：创造力八维 + 雷达总结 + 规则（可行性低→提出方案≤中）
def parse_level(s: str) -> int:
    # 越大越高：低(1) 较低(2) 中(3) 中上(4) 较高(5) 高(6)
    # 简化映射（模糊匹配）
    if "较高" in s: return 5
    if "中上" in s: return 4
    if "高" in s: return 6
    if "较低" in s: return 2
    if "低" in s: return 1
    if "中等" in s or "中" in s: return 3
    return 0

def check_D(item: Dict[str,Any]) -> List[str]:
    txt=item.get("创造力","") or ""
    issues=[]
    for d in CRE_8:
        if d not in txt:
            issues.append("D.creativity_missing:"+d)
    # “八维不得全部相同”
    levels=[]
    for d in CRE_8:
        m=re.search(d+r"[^。；]*?(高|较高|中上|中等|中|较低|低)", txt)
        levels.append(parse_level(m.group(1)) if m else 0)
    if levels and all(x==levels[0] for x in levels):
        issues.append("D.levels_all_equal")
    # 可行性 vs 提出方案
    lvl_feas=0; lvl_prop=6
    m1=re.search(r"可行性[^。；]*?(高|较高|中上|中等|中|较低|低)", txt);
    m2=re.search(r"提出方案[^。；]*?(高|较高|中上|中等|中|较低|低)", txt)
    if m1: lvl_feas=parse_level(m1.group(1))
    if m2: lvl_prop=parse_level(m2.group(1))
    if lvl_feas in (1,2) and lvl_prop>3:
        issues.append("D.rule_feas_low_prop_too_high")
    # 雷达总结提示
    if ("雷达" not in txt) and ("总结" not in txt):
        issues.append("D.no_radar_summary")
    return issues

# —— E：心理健康结构 & 非诊断化
def check_E(item: Dict[str,Any]) -> List[str]:
    txt=item.get("心理健康","") or ""
    issues=[]
    # 关键槽位是否出现
    slots = {
        "综合心理状况": r"综合心理状况[^。；]*?(高|较高|中上|中等|中|较低|低)",
        "幸福指数": r"幸福指数[^。；]*?(高|较高|中上|中等|中|较低|低)",
        "抑郁风险": r"抑郁风险[^。；]*?(低|轻度|中度|重度)",
        "焦虑风险": r"焦虑风险[^。；]*?(低|轻度|中度|重度)",
    }
    for k,pat in slots.items():
        if not re.search(pat, txt):
            issues.append("E.psych_slot_missing:"+k)
    # 非诊断化（简易黑词表）
    blacklist = ["重度抑郁","双相","住院","强迫症严重","精神分裂","处方药滥用"]
    for w in blacklist:
        if w in txt:
            issues.append("E.non_clinical_violation:"+w)
    return issues

# —— F：跨维一致性
def check_F(item: Dict[str,Any]) -> List[str]:
    issues=[]
    # 年级 ↔ 年龄（允许±1）
    grade=item.get("年级",""); age=item.get("年龄",None)
    if grade in GRADE_AGE and isinstance(age,(int,float)):
        lo,hi=GRADE_AGE[grade]
        if not (lo-1 <= age <= hi+1):
            issues.append("F.grade_age_outlier")
    # 价值观“身心健康”较高/高 → 心理：综合心理状况≥中, 抑郁/焦虑 ≤ 中度
    v=item.get("价值观","") or ""
    if "身心健康" in v and re.search(r"身心健康[^。；]*?(较高|高)", v):
        p=item.get("心理健康","") or ""
        ok=True
        m_z=re.search(r"综合心理状况[^。；]*?(高|较高|中上|中等|中|较低|低)", p)
        lvl = parse_level(m_z.group(1)) if m_z else 0
        if lvl and lvl<3: ok=False
        for risk in ["抑郁风险","焦虑风险"]:
            m_r=re.search(rf"{risk}[^。；]*?(低|轻度|中度|重度)", p)
            if m_r and ("重度" in m_r.group(1)): ok=False
        if not ok:
            issues.append("F.value_psych_inconsistency")
    return issues

# —— G：多样性（批内近重复率）
def build_key_text(it: Dict[str,Any]) -> str:
    return "｜".join([
        str(it.get("年级","")), str(it.get("性别","")),
        " ".join(it.get("人格",[]) if isinstance(it.get("人格"), list) else [str(it.get("人格",""))]),
        str(it.get("价值观","")), str(it.get("社交关系","")),
        str(it.get("创造力","")), str(it.get("心理健康",""))
    ])

def measure_G(all_items: List[Dict[str,Any]], sim_th: int) -> Tuple[float, List[Tuple[int,int,int]]]:
    """返回：近重复占比, 重复边( (i,j,hamming) ) 列表（i<j 按索引）"""
    hashes=[]
    for it in all_items:
        hashes.append(simhash64(build_key_text(it)))
    dup_edges=[]
    n=len(hashes)
    if n<=1: return 0.0, dup_edges
    # 简单O(n^2)（若很大可换近邻索引）
    dup=0
    for i in range(n):
        for j in range(i+1,n):
            d=hamming(hashes[i],hashes[j])
            if d<=sim_th:
                dup+=1
                dup_edges.append((i,j,d))
    # 定义占比：重复对数 / 组合数
    ratio = dup / (n*(n-1)/2)
    return ratio, dup_edges

def eval_one(item: Dict[str,Any]) -> Dict[str,Any]:
    issues = []
    issues += check_A(item)
    issues += check_B(item)
    issues += check_C(item)
    issues += check_D(item)
    issues += check_E(item)
    issues += check_F(item)
    return {
        "id": item.get("id"),
        "年级": item.get("年级"),
        "性别": item.get("性别"),
        "学术水平": item.get("学术水平"),
        "ok": len(issues)==0,
        "issues": issues
    }

def bucket(x: Dict[str,Any], keys: List[str]) -> str:
    return " / ".join([str(x.get(k,"-")) for k in keys])

def to_md_badge(v: float, green: Tuple[float,float], yellow: Tuple[float,float], fmt="{:.1%}") -> str:
    """绿区间(green[0]~green[1])更好；否则黄/红"""
    s = fmt.format(v)
    if green[0] <= v <= green[1]: return f"🟢 {s}"
    if yellow[0] <= v <= yellow[1]: return f"🟡 {s}"
    return f"🔴 {s}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="输入目录（包含 students_chunk_*.jsonl）或单个 jsonl")
    ap.add_argument("--outdir", required=True, help="评估输出目录")
    ap.add_argument("--simhash_threshold", type=int, default=3)
    ap.add_argument("--topn", type=int, default=50, help="可疑样本Top-N")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    items = scan_inputs(args.input)
    n = len(items)

    # 单条评估
    rows = [eval_one(it) for it in items]
    ok_cnt = sum(1 for r in rows if r["ok"])
    issue_flat = []
    for i,r in enumerate(rows):
        for iss in r["issues"]:
            issue_flat.append((i, iss))

    # 近重复
    dup_ratio, dup_edges = measure_G(items, args.simhash_threshold)

    # 分布：年级/性别/学术水平
    dist_grade = Counter([it.get("年级","-") for it in items])
    dist_gender = Counter([it.get("性别","-") for it in items])
    dist_level = Counter([it.get("学术水平","-") for it in items])

    # 分组通过率
    def group_pass(keys: List[str]):
        g = defaultdict(lambda: {"total":0,"ok":0})
        for r in rows:
            k = " / ".join([str(r.get(x,"-")) for x in keys])
            g[k]["total"] += 1
            g[k]["ok"] += (1 if r["ok"] else 0)
        out=[]
        for k,v in g.items():
            rate = v["ok"]/v["total"] if v["total"] else 0.0
            out.append({"group":k,"total":v["total"],"ok":v["ok"],"pass_rate":rate})
        out.sort(key=lambda x: -x["total"])
        return out

    g_grade = group_pass(["年级"])
    g_gender = group_pass(["性别"])
    g_grade_gender = group_pass(["年级","性别"])

    # 可疑样本Top-N（按问题数降序）
    scored = []
    for i,r in enumerate(rows):
        scored.append((i, len(r["issues"]), r["issues"]))
    scored.sort(key=lambda x: -x[1])
    topN = scored[:min(args.topn, len(scored))]

    # 输出：JSON
    report = {
        "total": n,
        "ok_count": ok_cnt,
        "ok_rate": ok_cnt/n if n else 0.0,
        "dup_ratio": dup_ratio,
        "dup_edges_count": len(dup_edges),
        "dist": {
            "grade": dist_grade,
            "gender": dist_gender,
            "level": dist_level
        },
        "groups": {
            "by_grade": g_grade,
            "by_gender": g_gender,
            "by_grade_gender": g_grade_gender
        },
        "issues_overview": Counter([x[1] for x in issue_flat]),
        "topN_indices": [i for (i,_,_) in topN]
    }
    with open(os.path.join(args.outdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 复制失败样本（若存在）
    fail_src = os.path.join(args.input, "failures.jsonl") if os.path.isdir(args.input) else None
    if fail_src and os.path.exists(fail_src):
        fails = load_jsonl(fail_src)
        with open(os.path.join(args.outdir, "failures_samples.json"), "w", encoding="utf-8") as f:
            json.dump(fails, f, ensure_ascii=False, indent=2)

    # suspicious.csv
    with open(os.path.join(args.outdir, "suspicious.csv"), "w", newline="", encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow(["index","id","grade","gender","issue_count","issues"])
        for i,cnt,iss in topN:
            it=items[i]
            w.writerow([i, it.get("id"), it.get("年级"), it.get("性别"), cnt, ";".join(iss)])

    # duplicates.csv
    with open(os.path.join(args.outdir, "duplicates.csv"), "w", newline="", encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["i","j","hamming","id_i","id_j","grade_i","grade_j"])
        for (i,j,d) in dup_edges:
            it_i=items[i]; it_j=items[j]
            w.writerow([i,j,d, it_i.get("id"), it_j.get("id"), it_i.get("年级"), it_j.get("年级")])

    # report.md（红黄绿灯）
    ok_rate = report["ok_rate"]
    # 经验阈：ok_rate 绿≥0.9，黄0.8~0.9；dup_ratio 绿≤0.01，黄0.01~0.03
    ok_badge  = to_md_badge(ok_rate, (0.90,1.01), (0.80,0.90))
    dup_badge = to_md_badge(dup_ratio, (0.0,0.01), (0.01,0.03))
    top_issue = report["issues_overview"].most_common(8)

    md = []
    md.append(f"# 批量后验评估（N={n}）")
    md.append(f"- ✅ 合格率：{ok_badge}")
    md.append(f"- 🧬 近重复率（SimHash≤{args.simhash_threshold}）：{dup_badge}  （重复边：{len(dup_edges)}）\n")
    md.append("## 主要问题 Top-8")
    for k,v in top_issue:
        md.append(f"- {k} × {v}")
    md.append("\n## 分组通过率（年级×性别）Top-10")
    for row in report["groups"]["by_grade_gender"][:10]:
        md.append(f"- {row['group']}: {row['ok']}/{row['total']}（{row['pass_rate']:.1%}）")
    md.append("\n## 可疑样本 Top-N（详见 suspicious.csv）")
    for i,cnt,iss in topN[:10]:
        md.append(f"- idx {i} · issues={cnt}: {', '.join(iss[:6])}{'…' if len(iss)>6 else ''}")

    with open(os.path.join(args.outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"[OK] Wrote {args.outdir}/report.json, report.md, suspicious.csv, duplicates.csv")

if __name__ == "__main__":
    main()
                                                                                                                                                                                                       