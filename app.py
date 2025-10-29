# -*- coding: utf-8 -*-
"""
app.py — 多智能体 · 实时多轮协作 · 学生画像批量生成（全部字段由智能体经API产出）
在线前置控制：配额分桶调度 + 轻量过滤 + 自相似度阈（SimHash）
后端：Orchestrator + 5 内容Agent + Validator，白板黑板模式 + 多轮协商
依赖：streamlit, requests
接口：使用 AIECNU /v1/chat/completions（已硬编码）
!!! 功能要点：
1) 无生成上限：按 50 条/片 分片，支持任意大 N；顶栏“分片进度”，中部“当前片进度”；
2) 无协商轮数上限：轮数 number_input（不设上限）；
3) 暂停/继续：可随时暂停；暂停即作废“当前正在构建”的条目；继续自动找到最后一片并续写；
4) 自动落盘：生成一条就写一条到本地 `output/<run_id>/students_chunk_{i}.jsonl`；50条为一片，自动换新文件；
5) 体裁新标准：价值观/创造力/心理健康 强制“单段连续自然语言”；一致性与合规校验；
6) 学术水平严格“四选一（固定文案）”；代理名允许多音节（姓1–2音节、名1–3音节，每节拼音+1~5声调，用下划线分隔）；
7) QuotaScheduler（年级×性别×优势学科簇）前置采样；轻量过滤；SimHash 去同质化；失败样本落盘；
8) 🖧 实时交互控制台（Prompt/Output/Issues 可视化）；
9) ★ 新增：学术水平分布锚定 + 跨维度“乐观偏置”抑制（价值观/创造力/心理健康随锚自适应，并在轻量过滤中做硬阈校验）。
"""

import json, re, random, math, os, glob, time, hashlib
from copy import deepcopy
from typing import Any, Dict, List, Tuple, Optional
import streamlit as st
import requests

# ================== 你的 API（按要求硬编码） ==================
AIECNU_API_KEY = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
AIECNU_BASE_URL = "http://xxxxxxxxxxx"
MODEL = "gpt-4.1"
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {AIECNU_API_KEY}"}

CHUNK_SIZE_DEFAULT = 50
MAX_RETRIES_PER_SLOT = 4
SIMHASH_BITS = 64
SIMHASH_HAMMING_THRESHOLD_DEFAULT = 3  # <=3 视为过近，重采

LEVEL_SET_STRICT = {
    "高": "高：成绩全校排名前10%",
    "中": "中：成绩全校排名前10%至30%",
    "低": "低：成绩全校排名前30%至50%",
    "差": "差：成绩全校排名后50%"
}
STRICT_ALLOWED_STRINGS = set(LEVEL_SET_STRICT.values())
LEVELS = [
    "高：成绩全校排名前10%",
    "中：成绩全校排名前10%至30%",
    "低：成绩全校排名前30%至50%",
    "差：成绩全校排名后50%",
]
LEVEL_ALIAS = {
    "高": LEVELS[0], "中": LEVELS[1], "低": LEVELS[2], "差": LEVELS[3],
    "high": LEVELS[0], "mid": LEVELS[1], "medium": LEVELS[1], "low": LEVELS[2], "poor": LEVELS[3]
}

# 代理名校验正则（支持 ≥2 音节；姓1-2节，名1-3节；每节[a-z]++声调1-5；姓与名之间一个下划线）
AGENT_ID_REGEX = r"^(?:[a-z]+[1-5]){1,2}_(?:[a-z]+[1-5]){1,3}$"

GRADES = ["一年级","二年级","三年级","四年级","五年级","六年级","初一","初二","初三","高一","高二","高三"]
GENDERS = ["男","女"]
SUBJ_CLUSTERS = {
    "理科向": ["数学","物理","化学","信息技术"],
    "文社向": ["语文","历史","政治","地理"],
    "艺体向": ["美术","音乐","体育"],
    "外语生物向": ["英语","生物"]
}

# ---- streamlit rerun 兼容封装 ----
def _st_rerun():
    try:
        st.rerun()
    except AttributeError:
        st.experimental_rerun()

# ================== 工具：解析学术水平比例 ==================
def _parse_level_mix(text: str) -> Dict[str, float]:
    """
    解析用户输入的学术水平配比字符串，如：
      高:0.25,中:0.25,低:0.25,差:0.25
      或英文别名：high:0.4,mid:0.3,low:0.2,poor:0.1
    返回严格四选一文案的比例字典；非法或缺失自动均分。
    """
    default = {LEVELS[0]:0.25, LEVELS[1]:0.25, LEVELS[2]:0.25, LEVELS[3]:0.25}
    if not text:
        return default
    try:
        kvs = [x.strip() for x in text.split(",") if x.strip()]
        acc = {}
        for kv in kvs:
            if ":" not in kv:
                continue
            k, v = [t.strip() for t in kv.split(":", 1)]
            k_std = LEVEL_ALIAS.get(k, k)
            if k_std not in STRICT_ALLOWED_STRINGS:
                continue
            acc[k_std] = float(v)
        if not acc: return default
        s = sum(acc.values())
        if s <= 0: return default
        for k in list(acc.keys()):
            acc[k] = acc[k] / s
        for l in LEVELS:
            acc.setdefault(l, 0.0)
        return acc
    except:
        return default

# ================== LLM 调用与解析 ==================
def call_llm(messages: List[Dict[str, Any]], max_tokens=900, temperature=0.95) -> str:
    url = f"{AIECNU_BASE_URL.rstrip('/')}/chat/completions"
    payload = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

def try_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except:
                return {}
        return {}

def non_empty(v: Any) -> bool:
    if v is None: return False
    if isinstance(v, str): return v.strip() != ""
    if isinstance(v, (list, dict)): return len(v) > 0
    return True

# ================== SimHash（去同质化） ==================
def _text_to_ngrams(t: str, n: int = 3) -> List[str]:
    t = re.sub(r"\s+", "", t)
    return [t[i:i+n] for i in range(max(0, len(t)-n+1))] if t else []

def _simhash64(text: str) -> int:
    v = [0]*SIMHASH_BITS
    for g in _text_to_ngrams(text, 3):
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        for i in range(SIMHASH_BITS):
            v[i] += 1 if ((h >> i) & 1) else -1
    out = 0
    for i in range(SIMHASH_BITS):
        if v[i] >= 0:
            out |= (1 << i)
    return out

def _hamming(a: int, b: int) -> int:
    x = a ^ b
    cnt = 0
    while x:
        x &= x-1
        cnt += 1
    return cnt

class SimilarityGate:
    def __init__(self, threshold: int = SIMHASH_HAMMING_THRESHOLD_DEFAULT):
        self.threshold = threshold
        self.pool: List[int] = []

    def too_similar(self, text: str) -> bool:
        if not text: return False
        h = _simhash64(text)
        for prev in self.pool:
            if _hamming(h, prev) <= self.threshold:
                return True
        return False

    def accept(self, text: str):
        if not text: return
        self.pool.append(_simhash64(text))

# ================== 轻量过滤（体裁/正则/显式命中 + 乐观偏置抑制） ==================
AGENT_PARAGRAPH_FIELDS = ["价值观","创造力","心理健康"]
VAL_DIMS7 = ["道德修养","身心健康","法治意识","社会责任","政治认同","文化素养","家庭观念"]
LVL_WORDS = ["高","较高","中上","中","较低","低"]
CRE_DIMS8 = ["流畅性","新颖性","灵活性","可行性","问题发现","问题分析","提出方案","改善方案"]
PSY_KEYS  = ["综合心理状况","幸福指数","抑郁风险","焦虑风险"]

def _is_single_paragraph(s: str) -> bool:
    if not isinstance(s, str): return False
    if re.search(r"(\n\s*\n)|(^\s*[-•\d]+\.)", s): return False
    return True

def _has_any(s: str, kws: List[str]) -> bool:
    return any(kw in s for kw in kws)

def _count_levels(text: str) -> Dict[str, int]:
    cnt = {k:0 for k in LVL_WORDS}
    for k in LVL_WORDS:
        cnt[k] = len(re.findall(re.escape(k), text))
    return cnt

def _count_lowish(text: str) -> int:
    # 统计“中/较低/低”（不含“中上”）
    n_mid = len(re.findall(r"(?<!中)中(?!上)", text))
    n_low = len(re.findall(r"较低|低", text))
    return n_mid + n_low

def _extract_dim_levels(text: str, dims: List[str]) -> Dict[str, str]:
    """
    近似抽取每个维度的等级词（正则启发式），用于八维/七维粗校验。
    """
    res = {}
    for d in dims:
        # 维度名后若干字符内的等级词
        m = re.search(d + r".{0,12}?(高|较高|中上|中(?!上)|较低|低)", text)
        if m: res[d] = m.group(1)
    return res

def _light_filter(item: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons = []
    # 1) 必填键
    for k in ["姓名","年龄","性别","年级","人格","擅长科目","薄弱科目","学术水平","价值观","创造力","心理健康","代理名","发展阶段","社交关系"]:
        if k not in item or not non_empty(item[k]):
            reasons.append(f"缺字段或为空：{k}")

    # 2) 学术水平+代理名+段落体裁
    if item.get("学术水平") not in STRICT_ALLOWED_STRINGS:
        reasons.append("学术水平非四选一固定文案")
    if not re.match(AGENT_ID_REGEX, str(item.get("代理名",""))):
        reasons.append("代理名不合规（需拼音分节+声调数字；姓1-2节，名1-3节）")
    for f in AGENT_PARAGRAPH_FIELDS:
        if not _is_single_paragraph(item.get(f,"")):
            reasons.append(f"{f} 非单段体裁")

    # 3) 价值观：七维&等级词 + 乐观偏置抑制
    val = item.get("价值观","")
    if not _has_any(val, VAL_DIMS7): reasons.append("价值观未见七维显式名词（至少缺大部分）")
    if not _has_any(val, LVL_WORDS): reasons.append("价值观未见等级词")
    # 锚定驱动的下调要求
    target = item.get("_采样约束",{}).get("目标学术水平") if isinstance(item.get("_采样约束"), dict) else None
    lowish_need = 0
    if target in [LEVELS[1]]:      # 中
        lowish_need = 1
    elif target in [LEVELS[2]]:    # 低
        lowish_need = 2
    elif target in [LEVELS[3]]:    # 差
        lowish_need = 3
    if lowish_need>0 and _count_lowish(val) < lowish_need:
        reasons.append(f"价值观等级分布过高（锚={target or '无'}）：需要≥{lowish_need}处“中/较低/低”")

    # 4) 创造力：八维 + 雷达 + 乐观偏置抑制 + 内部一致性
    cre = item.get("创造力","")
    if not _has_any(cre, CRE_DIMS8): reasons.append("创造力未见八维显式名词（至少缺大部分）")
    if "雷达" not in cre and "总结" not in cre: reasons.append("创造力未见雷达总结提示词")
    dimlv = _extract_dim_levels(cre, CRE_DIMS8)
    # 至少 N 个维度为“中及以下”（不含“中上”）
    lowish_cre = sum(1 for v in dimlv.values() if v in ["中","较低","低"])
    need = 0
    if target in [LEVELS[1]]:  # 中
        need = 2
    elif target in [LEVELS[2]]:  # 低
        need = 3
    elif target in [LEVELS[3]]:  # 差
        need = 4
    if need>0 and lowish_cre < need:
        reasons.append(f"创造力八维整体偏高（锚={target or '无'}）：要求≥{need}个维度为“中及以下”，当前={lowish_cre}")
    # 原有一致性：可行性低→提出方案≤中
    if ("可行性" in dimlv and dimlv.get("可行性") in ["较低","低"]) and \
       ("提出方案" in dimlv and dimlv.get("提出方案") in ["高","较高","中上"]):
        reasons.append("创造力一致性：可行性低但‘提出方案’高")

    # 5) 心理健康：关键槽位 + 乐观偏置抑制（四槽位至少出现中/较低/低；风险不能全低于‘中’的反面）
    psy = item.get("心理健康","")
    if not _has_any(psy, ["综合心理状况","幸福指数","抑郁风险","焦虑风险","信息不足或未见显著症状","背景","应对","支持","家庭","同伴","老师"]):
        reasons.append("心理健康未见核心槽位关键词")
    # 粗抽四槽位等级
    psy_map = {}
    for k in PSY_KEYS:
        m = re.search(k + r".{0,12}?(高|较高|中上|中(?!上)|较低|低|轻度|中度|重度|低风险)", psy)
        if m: psy_map[k] = m.group(1)
    if target in [LEVELS[2], LEVELS[3]]:  # 低/差
        # 要求：综合心理状况/幸福指数 至少一个为“中及以下”；抑郁/焦虑风险不得都写成“低/低风险”
        cnt_mid_or_low = sum(1 for k in ["综合心理状况","幸福指数"] if psy_map.get(k) in ["中","较低","低"])
        if cnt_mid_or_low < 1:
            reasons.append("心理健康与锚不符：综合心理状况/幸福指数至少1处需“中或较低/低”")
        risk_lowish = 0
        for k in ["抑郁风险","焦虑风险"]:
            v = psy_map.get(k, "")
            if any(x in v for x in ["轻度","中度"]):  # 允许轻/中
                risk_lowish += 1
        # 若两项都显式“低/低风险”，在‘低/差’锚下不合理
        if "抑郁风险" in psy_map and "焦虑风险" in psy_map:
            both_low = all(("低" in psy_map[k] or "低风险" in psy_map[k]) for k in ["抑郁风险","焦虑风险"])
            if both_low:
                reasons.append("心理健康与锚不符：抑郁/焦虑风险不应双双为‘低/低风险’")
    return len(reasons) == 0, reasons

# ================== 协作基石：白板与讨论（支持IO日志） ==================
REQUIRED_KEYS = ["id","姓名","年龄","擅长科目","薄弱科目","年级","人格","社交关系",
                 "学术水平","性别","发展阶段","代理名","价值观","创造力","心理健康"]

class Whiteboard:
    def __init__(self, sid: int, sampling_hint: Optional[Dict[str,Any]] = None):
        self.facts: Dict[str, Any] = {"id": sid}
        if sampling_hint:
            self.facts["_采样约束"] = sampling_hint
        self.discussion: List[Dict[str, str]] = []

    def read(self) -> Dict[str, Any]: return deepcopy(self.facts)
    def write(self, patch: Dict[str, Any]):
        for k,v in patch.items():
            self.facts[k] = v

    def log(self, speaker: str, content: str):
        self.discussion.append({"speaker": speaker, "content": content})

    def serialize_for_agent(self) -> str:
        return json.dumps({"draft": self.facts, "discussion": self.discussion}, ensure_ascii=False)

# ================== 基础提示词：统一协议 ==================
AGENT_PREAMBLE = """你是一个与其他智能体协作的“学生画像”生产成员。我们使用“公共白板”共享草稿与讨论。
规则（必须遵守）：
- 所有输出必须是 **合法 JSON 对象**，且只包含你负责的键。
- 不得引用模板句式；用自然中文；避免空话套话；避免与白板草稿自相矛盾。
- 若被要求修订，只改你负责的键；不留空；保证与其它字段逻辑一致。
- 姓名等中文；数字与百分位请用中文语境书写（如“前10%”）。
- 不要输出任何多余说明文字。只输出 JSON。
- 如白板中存在“_采样约束”，请严格遵循其中的“年级”“性别”“优势学科偏向”“目标学术水平”等要求；若发生冲突，以采样约束为准并保持整体一致性。
"""

RESP_FIELDS = {
    "学籍与发展阶段": ["姓名","年龄","性别","年级","发展阶段","代理名"],
    "学业画像": ["擅长科目","薄弱科目","学术水平"],
    "人格与价值观": ["人格","价值观"],
    "社交与创造力": ["社交关系","创造力"],
    "身心健康": ["心理健康"]
}

def _pack_prompt(instruction: str, wb: Whiteboard) -> str:
    return f"【INSTRUCTION】\n{instruction}\n\n【WHITEBOARD】\n{wb.serialize_for_agent()}"

# ================== 各 Agent（含自适应锚指引 + IO日志） ==================
def agent_scholar(wb: Whiteboard, seed: str, mode: str="propose") -> Dict[str,Any]:
    sampling = wb.read().get("_采样约束", {})
    hint = ""
    if sampling:
        hint = f"\n采样约束（遵循）：年级={sampling.get('年级','未指定')}，性别={sampling.get('性别','未指定')}，目标学术水平={sampling.get('目标学术水平','无')}。"
    instruction = f"""{AGENT_PREAMBLE}{hint}
你负责键：{RESP_FIELDS["学籍与发展阶段"]}
任务模式：{mode}
多样性种子：{seed}

生成与约束（必须）：
- 年龄 6~18；年龄**必须是一个阿拉伯数字**，年级与年龄匹配（允许±1年跳级/留级但需与其他段落一致）；
- 发展阶段对象必须含三键：皮亚杰认知发展阶段、埃里克森心理社会发展阶段、科尔伯格道德发展阶段；
- 代理名格式（**多音节支持**）：姓 1~2 音节、名 1~3 音节；每个音节为“拼音小写+声调数字(1-5)”；姓与名之间用下划线；示例：
  - 单姓单名：zhang1_shuang3
  - 单姓双名：li1_huan4ying1
  - 复姓双名：ou3yang2_ming2hao3
仅输出 JSON。
"""
    messages = [
        {"role":"developer","content":instruction},
        {"role":"user","content":f"公共白板：\n{wb.serialize_for_agent()}\n请仅输出你负责的 JSON。"}
    ]
    wb.log("学籍与发展阶段→prompt", _pack_prompt(instruction, wb))
    out = call_llm(messages, max_tokens=700, temperature=0.98)
    wb.log("学籍与发展阶段←output", out)
    return try_json(out)

def agent_academic(wb: Whiteboard, seed: str, mode: str="propose") -> Dict[str,Any]:
    sampling = wb.read().get("_采样约束", {})
    prefer = sampling.get("优势学科偏向")
    prefer_str = f"请优先使“擅长科目”覆盖该簇中的至少1门：{prefer}。" if prefer else ""
    target_level = sampling.get("目标学术水平")
    target_line = f"【强约束】本样本的“学术水平”必须严格等于：{target_level}；不得改为其它档位。" if target_level else "（无目标锚）"

    instruction = f"""{AGENT_PREAMBLE}
你负责键：{RESP_FIELDS["学业画像"]}
任务模式：{mode}
多样性种子：{seed}

要求（必须）：
- “擅长科目”与“薄弱科目”均为非空数组，且两者**集合不相交**；
- “学术水平”**严格四选一，且字符串必须完全等于以下之一**：
  1) "高：成绩全校排名前10%"
  2) "中：成绩全校排名前10%至30%"
  3) "低：成绩全校排名前30%至50%"
  4) "差：成绩全校排名后50%"
- {prefer_str}
- {target_line}

仅输出 JSON（只含“擅长科目”“薄弱科目”“学术水平”三个键）。
"""
    messages = [
        {"role":"developer","content":instruction},
        {"role":"user","content":f"公共白板：\n{wb.serialize_for_agent()}\n请仅输出你负责的 JSON。"}
    ]
    wb.log("学业画像→prompt", _pack_prompt(instruction, wb))
    out = call_llm(messages, max_tokens=600, temperature=0.9)
    wb.log("学业画像←output", out)
    data = try_json(out)

    # 兜底归一 + 强制对齐目标锚（如存在）
    if isinstance(data, dict):
        lvl = data.get("学术水平")
        if isinstance(lvl, str):
            for k, v in LEVEL_SET_STRICT.items():
                if lvl.startswith(k) or k in lvl:
                    data["学术水平"] = v; break
        if target_level and data.get("学术水平") != target_level:
            data["学术水平"] = target_level
    return data if isinstance(data, dict) else {}

def agent_values(wb: Whiteboard, seed: str, mode: str="propose") -> Dict[str,Any]:
    sampling = wb.read().get("_采样约束", {})
    target = sampling.get("目标学术水平")
    adapt = ""
    if target in [LEVELS[1], LEVELS[2], LEVELS[3]]:
        adapt = ("- 【随学术锚自适应】当目标为“中/低/差”时，七维中的等级词应呈**不均衡但包含若干“中/较低/低”**，"
                 "避免全高/较高；并给出与之匹配的背景化根据（如学习习惯/反馈/社团表现等）。")
    instruction = f"""{AGENT_PREAMBLE}
你负责键：{RESP_FIELDS["人格与价值观"]}
任务模式：{mode}
多样性种子：{seed}

输出体裁（强约束）：单段连续自然语言；**覆盖七维并有等级词**（道德修养、身心健康、法治意识、社会责任、政治认同、文化素养、家庭观念）；给出背景化依据。
{adapt}
仅输出 JSON。
"""
    messages = [
        {"role":"developer","content":instruction},
        {"role":"user","content":f"公共白板：\n{wb.serialize_for_agent()}\n请仅输出你负责的 JSON。"}
    ]
    wb.log("人格与价值观→prompt", _pack_prompt(instruction, wb))
    out = call_llm(messages, max_tokens=900, temperature=1.0)
    wb.log("人格与价值观←output", out)
    return try_json(out)

def agent_social_creative(wb: Whiteboard, seed: str, mode: str="propose") -> Dict[str,Any]:
    sampling = wb.read().get("_采样约束", {})
    target = sampling.get("目标学术水平")
    adapt = ""
    if target in [LEVELS[1], LEVELS[2], LEVELS[3]]:
        adapt = ("- 【随学术锚自适应】当目标为“中/低/差”时，八维等级分布**必须包含若干“中/较低/低”**（至少2/3/4个维度），"
                 "并保持可行性与提出方案的一致性；末尾雷达总结据此概括强弱。")
    instruction = f"""{AGENT_PREAMBLE}
你负责键：{RESP_FIELDS["社交与创造力"]}
任务模式：{mode}
多样性种子：{seed}

社交关系：单段（160~260字），背景→关键事件→影响；不得换行/条列。
创造力：单段；**八维（流畅性/新颖性/灵活性/可行性/问题发现/问题分析/提出方案/改善方案 各有等级词）+ 雷达总结**；八维不得全同档；若“可行性”较低/低，则“提出方案”不高于中等。
{adapt}
仅输出 JSON。
"""
    messages = [
        {"role":"developer","content":instruction},
        {"role":"user","content":f"公共白板：\n{wb.serialize_for_agent()}\n请仅输出你负责的 JSON。"}
    ]
    wb.log("社交与创造力→prompt", _pack_prompt(instruction, wb))
    out = call_llm(messages, max_tokens=1100, temperature=1.02)
    wb.log("社交与创造力←output", out)
    return try_json(out)

def agent_health(wb: Whiteboard, seed: str, mode: str="propose") -> Dict[str,Any]:
    sampling = wb.read().get("_采样约束", {})
    target = sampling.get("目标学术水平")
    adapt = ""
    if target in [LEVELS[2], LEVELS[3]]:
        adapt = ("- 【随学术锚自适应】当目标为“低/差”时，“综合心理状况/幸福指数”中至少一项宜为“中或较低/低”；"
                 "抑郁/焦虑风险避免双双‘低’；仍须保持**非诊断化**与“可支持、可改善”的教育语境。")
    instruction = f"""{AGENT_PREAMBLE}
你负责键：{RESP_FIELDS["身心健康"]}
任务模式：{mode}
多样性种子：{seed}

心理健康：单段；依次内嵌 概述→性格特征(≥2)→综合心理状况/幸福指数/抑郁风险/焦虑风险→心理疾病（如无写“信息不足或未见显著症状”）→背景故事→支撑与应对；非诊断化；与价值观“身心健康”一致。
{adapt}
仅输出 JSON。
"""
    messages = [
        {"role":"developer","content":instruction},
        {"role":"user","content":f"公共白板：\n{wb.serialize_for_agent()}\n请仅输出你负责的 JSON。"}
    ]
    wb.log("身心健康→prompt", _pack_prompt(instruction, wb))
    out = call_llm(messages, max_tokens=1100, temperature=0.96)
    wb.log("身心健康←output", out)
    return try_json(out)

def agent_validator(wb: Whiteboard, seed: str) -> Dict[str, Any]:
    instruction = f"""你是“Validator”智能体。请严格审校并给出**结构化修订任务**。
{AGENT_PREAMBLE}
你只输出 JSON，键为 issues 与 final_ready。不要输出多余文字。
"""
    rules = f"""规则参考（必须）：
- R1 年龄↔年级常模：6-7一年级；7-8二；8-9三；9-10四；10-11五；11-12六；12-13初一；13-14初二；14-15初三；15-16高一；16-17高二；17-18高三（允许±1年内偏差）。
- R2 发展阶段与年龄：~12岁以下多为“具体运算”；~12岁以上“形式运算”。埃里克森：6-12勤奋vs自卑；12-18身份vs角色混乱；科尔伯格：~10前习俗、~10-15习俗、≥15可向后习俗过渡。
- R3 科目集合不交叉、且均非空。
- R4 创造力八维等级需有起伏，避免全部相同；若“可行性”较低/低，则“提出方案”不高于中等。
- R5 价值观积极稳健时，心理段落不得出现严重功能受损或重度临床术语。
- R6 代理名正则：^[a-z]+[1-5]?_[a-z]+[1-5]?$
- R7 所有必填键不可为空：id, 姓名, 年龄, 擅长科目, 薄弱科目, 年级, 人格, 社交关系, 学术水平, 性别, 发展阶段, 代理名, 价值观, 创造力, 心理健康。
- R8 价值观：必须覆盖七维（道德修养/身心健康/法治意识/社会责任/政治认同/文化素养/家庭观念），每维含可识别等级词；允许自然顺序与自由句法，但需可定位。
- R9 创造力：必须含 概述 + 八维（流畅性/新颖性/灵活性/可行性/问题发现/问题分析/提出方案/改善方案，逐维有等级词与简短依据）+ 雷达总结。
- R10 心理健康：必须含 概述 + 性格特征(≥2点) + 三维度（综合心理状况/幸福指数/抑郁风险与焦虑风险） + 心理疾病（若无写“信息不足或未见显著症状”，若有写“诊断或倾向/功能影响/当前支持与处理”） + 背景故事 + 支撑与应对。
- R11 一致性：
    · 若价值观“身心健康”为“较高/高”，则心理“综合心理状况”≥中等，且“抑郁/焦虑风险”≤中度；如涉及疾病，需“已管理、功能基本稳定”；
    · 家庭观念较高与独立性不冲突，应呈现“互动支持、边界清晰”；
    · 价值观/社交/学业叙事互相支撑，不得矛盾（如社交回避 vs 频繁协作）。
- R12 非诊断化语言：避免“重度抑郁/双相/用药/住院”等重临床表述；允许“倾向/轻度/节点性/阶段性/可管理/建议咨询”等。
- R13 可读性与避免模板：内容应自然连贯，拒绝流水账与机械复述；若“等级词”缺失或维度缺失，提出修订。
- R14 段落化体裁：价值观/创造力/心理健康必须为**单段连续自然语言**，不得使用列表、编号、项目符号或多段换行；如检测到“\\n\\n”、“1.”、“- ”、“• ”等条列痕迹，应要求对应Owner重写为单段。
- R15 若“学术水平”不在允许集合，必须要求“学业画像”Owner重写并替换为**严格四选一固定文案**。
输出：issues: [{{code, desc, owner, fields, hint}}], final_ready: bool
"""
    messages = [
        {"role":"developer","content":instruction},
        {"role":"user","content":f"公共白板：\n{wb.serialize_for_agent()}\n{rules}\n请输出 JSON。"}
    ]
    wb.log("Validator→prompt", _pack_prompt(instruction + "\n\n" + rules, wb))
    out = call_llm(messages, max_tokens=1100, temperature=0.2)
    wb.log("Validator←output", out)
    data = try_json(out)
    # 本地兜底（学术水平、代理名、与目标锚一致性）
    try:
        lvl = wb.read().get("学术水平", "")
        if lvl not in STRICT_ALLOWED_STRINGS:
            issues = data.get("issues", []) if isinstance(data, dict) else []
            issues.append({
                "code":"R14",
                "desc":"学术水平未严格匹配允许集合。",
                "owner":"学业画像",
                "fields":["学术水平"],
                "hint":"替换为四选一固定文案：'高：成绩全校排名前10%' / '中：成绩全校排名前10%至30%' / '低：成绩全校排名前30%至50%' / '差：成绩全校排名后50%'"})
            data = {"issues": issues, "final_ready": False}
        agent_id = wb.read().get("代理名", "")
        if not re.match(AGENT_ID_REGEX, str(agent_id)):
            issues = data.get("issues", []) if isinstance(data, dict) else []
            issues.append({
                "code":"R6",
                "desc":"代理名不合规（应为多音节拼音+声调数字，姓1-2节，名1-3节，姓与名用下划线分隔）。",
                "owner":"学籍与发展阶段",
                "fields":["代理名"],
                "hint":"示例：zhang1_shuang3 / li1_huan4ying1 / ou3yang2_ming2hao3"})
            data = {"issues": issues, "final_ready": False}
        sampling = wb.read().get("_采样约束", {})
        target_level = sampling.get("目标学术水平")
        if target_level and lvl != target_level:
            issues = data.get("issues", []) if isinstance(data, dict) else []
            issues.append({
                "code":"R14-anchored",
                "desc":f"与采样目标学术水平不一致（期望：{target_level}，实际：{lvl}）。",
                "owner":"学业画像",
                "fields":["学术水平"],
                "hint":f"将“学术水平”改为目标档位：{target_level}；其余字段做轻微一致性修订。"})
            data = {"issues": issues, "final_ready": False}
    except Exception:
        pass
    try:
        wb.log("Validator(issues)", json.dumps(data.get("issues", []), ensure_ascii=False))
    except Exception:
        wb.log("Validator(issues)", "[]")
    return data if data else {"issues":[{"code":"SYS","desc":"解析失败，请各Agent自检并重述其负责字段。","owner":"学籍与发展阶段","fields":["姓名"],"hint":"重新完整给出。"}],"final_ready":False}

# ================== Orchestrator ==================
class Orchestrator:
    def __init__(self, max_rounds:int=3):
        self.max_rounds = max_rounds
        self.used_names: set = set()

    def _seed(self) -> str:
        return f"SEED-{random.randrange(10**16,10**17-1)}"

    def _merge_and_log(self, wb: Whiteboard, patch: Dict[str, Any], agent_name: str):
        wb.write(patch)
        wb.log(agent_name+"(合并)", json.dumps(patch, ensure_ascii=False))

    def run_one(self, sid: int, sampling_hint: Optional[Dict[str,Any]] = None) -> Tuple[Dict[str, Any], List[Dict[str,str]]]:
        wb = Whiteboard(sid, sampling_hint=sampling_hint)
        wb.log("System", f"以下姓名已被使用（请避免重复）：{list(self.used_names)}")
        seed = self._seed()

        self._merge_and_log(wb, agent_scholar(wb, seed, "propose"), "学籍与发展阶段")
        name_now = wb.read().get("姓名")
        if name_now: self.used_names.add(name_now)

        self._merge_and_log(wb, agent_academic(wb, seed, "propose"), "学业画像")
        self._merge_and_log(wb, agent_values(wb, seed, "propose"), "人格与价值观")
        self._merge_and_log(wb, agent_social_creative(wb, seed, "propose"), "社交与创造力")
        self._merge_and_log(wb, agent_health(wb, seed, "propose"), "身心健康")

        for r in range(1, self.max_rounds+1):
            v = agent_validator(wb, self._seed())
            issues = v.get("issues", [])
            final_ready = bool(v.get("final_ready", False))
            if final_ready and not issues:
                wb.log("Orchestrator", f"第{r}轮：Validator通过✅ 无需继续修订。")
                break
            wb.log("Orchestrator", f"第{r}轮：收到 {len(issues)} 个修订任务。")
            owners = {
                "学籍与发展阶段": agent_scholar,
                "学业画像": agent_academic,
                "人格与价值观": agent_values,
                "社交与创造力": agent_social_creative,
                "身心健康": agent_health
            }
            wb.log("Validator(issues)", json.dumps(issues, ensure_ascii=False))
            touched = set()
            for it in issues:
                owner = it.get("owner")
                if owner in owners and owner not in touched:
                    patched = owners[owner](wb, self._seed(), "revise")
                    self._merge_and_log(wb, patched, owner+"(revise)")
                    touched.add(owner)

        final = wb.read()
        missing = [k for k in REQUIRED_KEYS if not non_empty(final.get(k))]
        if missing:
            wb.log("Orchestrator", f"最终补齐：缺失 {missing}")
            for k in missing:
                owner = next((owner for owner,keys in RESP_FIELDS.items() if k in keys), None)
                if owner == "学籍与发展阶段":
                    self._merge_and_log(wb, agent_scholar(wb, self._seed(), "revise"), "学籍与发展阶段(revise-final)")
                elif owner == "学业画像":
                    self._merge_and_log(wb, agent_academic(wb, self._seed(), "revise"), "学业画像(revise-final)")
                elif owner == "人格与价值观":
                    self._merge_and_log(wb, agent_values(wb, self._seed(), "revise"), "人格与价值观(revise-final)")
                elif owner == "社交与创造力":
                    self._merge_and_log(wb, agent_social_creative(wb, self._seed(), "revise"), "社交与创造力(revise-final)")
                elif owner == "身心健康":
                    self._merge_and_log(wb, agent_health(wb, self._seed(), "revise"), "身心健康(revise-final)")
            final = wb.read()

        for k in REQUIRED_KEYS:
            if not non_empty(final.get(k)):
                raise RuntimeError(f"字段仍为空：{k}")

        lvl = final.get("学术水平", "")
        if lvl not in STRICT_ALLOWED_STRINGS:
            raise RuntimeError("学术水平不符合严格四选一标准，请重试。")

        # 注意：在落盘前保留 _采样约束 用于在线过滤的锚参考。落盘时你也可以选择 pop 掉。
        return final, wb.discussion

# ================== QuotaScheduler：按比例生成“目标学术水平” ==================
def _default_quota(n_total: int) -> List[Dict[str,Any]]:
    slots = []
    triplets = [(g,s,c) for g in GRADES for s in GENDERS for c in SUBJ_CLUSTERS.keys()]
    for i in range(n_total):
        g, s, c = triplets[i % len(triplets)]
        slots.append({"年级": g, "性别": s, "优势学科偏向": SUBJ_CLUSTERS[c]})
    random.shuffle(slots)
    return slots

def _cycle_levels_by_mix(n_total: int, mix: Dict[str, float]) -> List[str]:
    import math, random
    targets = []
    alloc = {k: int(round(mix.get(k,0.0) * n_total)) for k in STRICT_ALLOWED_STRINGS}
    diff = n_total - sum(alloc.values())
    if diff != 0:
        order = sorted(STRICT_ALLOWED_STRINGS, key=lambda k: mix.get(k,0.0), reverse=True)
        i = 0
        while diff != 0:
            k = order[i % len(order)]
            if diff > 0:
                alloc[k] += 1; diff -= 1
            else:
                if alloc[k] > 0:
                    alloc[k] -= 1; diff += 1
            i += 1
    for k, c in alloc.items():
        targets.extend([k] * max(0, c))
    random.shuffle(targets)
    if len(targets) < n_total:
        pad = list(STRICT_ALLOWED_STRINGS)
        while len(targets) < n_total:
            targets.append(random.choice(pad))
    return targets[:n_total]

class QuotaScheduler:
    def __init__(self, n_total: int, user_quota_json: Optional[str] = None, level_mix: Optional[Dict[str,float]] = None):
        if user_quota_json:
            try:
                arr = json.loads(user_quota_json)
                assert isinstance(arr, list) and all(isinstance(x, dict) for x in arr)
                self.slots = arr
            except Exception:
                self.slots = _default_quota(n_total)
        else:
            self.slots = _default_quota(n_total)
        mix = level_mix or {LEVELS[0]:0.25, LEVELS[1]:0.25, LEVELS[2]:0.25, LEVELS[3]:0.25}
        targets = _cycle_levels_by_mix(n_total, mix)
        for i, t in enumerate(targets):
            self.slots[i]["目标学术水平"] = t
        self.idx = 0
        self.total = n_total
    def has_next(self) -> bool:
        return self.idx < self.total
    def next_slot(self) -> Dict[str,Any]:
        if not self.has_next(): return {}
        slot = self.slots[self.idx]; self.idx += 1; return slot

# ================== 本地落盘（自动 JSONL） ==================
def _ensure_dirs():
    base = os.path.join(os.getcwd(), "output")
    if not os.path.exists(base): os.makedirs(base, exist_ok=True)
    return base

def _init_run_dir():
    base = _ensure_dirs()
    run_id = f"run_{int(time.time())}"
    run_dir = os.path.join(base, run_id)
    os.makedirs(run_dir, exist_ok=True)
    return run_id, run_dir

def _chunk_path(run_dir: str, chunk_no: int) -> str:
    return os.path.join(run_dir, f"students_chunk_{chunk_no}.jsonl")

def _append_record(run_dir: str, chunk_no: int, record: Dict[str, Any]):
    path = _chunk_path(run_dir, chunk_no)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def _append_failure(run_dir: str, failure: Dict[str, Any]):
    path = os.path.join(run_dir, "failures.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(failure, ensure_ascii=False) + "\n")

def _count_lines(path: str) -> int:
    if not os.path.exists(path): return 0
    cnt = 0
    with open(path, "r", encoding="utf-8") as f:
        for _ in f: cnt += 1
    return cnt

def _recover_progress_from_disk(run_dir: str, chunk_size: int) -> Tuple[int,int,int]:
    files = sorted(glob.glob(os.path.join(run_dir, "students_chunk_*.jsonl")))
    if not files: return 1, 0, 1
    def _num(p):
        m = re.search(r"students_chunk_(\d+)\.jsonl$", p)
        return int(m.group(1)) if m else 0
    files.sort(key=_num)
    last = files[-1]; last_no = _num(last)
    done_in_last = _count_lines(last)
    chunk_idx = last_no; in_chunk_idx = done_in_last
    global_idx = (chunk_idx-1)*chunk_size + in_chunk_idx + 1
    if in_chunk_idx >= chunk_size: chunk_idx += 1; in_chunk_idx = 0
    return chunk_idx, in_chunk_idx, global_idx

def _load_chunk_preview(run_dir: str, chunk_idx: int, max_items: int = 6) -> List[Dict[str, Any]]:
    path = _chunk_path(run_dir, chunk_idx)
    if not os.path.exists(path): return []
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: lines.append(json.loads(line))
            except: pass
    return lines[-max_items:]

# ================== UI ==================
st.set_page_config(page_title="多智能体画像生成（前置控制+交互控制台+分布锚定）", page_icon="🧩", layout="wide")
st.title("🧩 学生画像 · 多智能体实时协作（前置控制 + 交互控制台 + 分布锚定）")

with st.sidebar:
    st.subheader("在线前置控制")
    simhash_th = st.number_input("相似度阈（SimHash汉明距离，≤视为过近需重生）", 0, 16, SIMHASH_HAMMING_THRESHOLD_DEFAULT)
    user_quota_json = st.text_area("自定义配额JSON（可选）", placeholder='[{"年级":"初一","性别":"女","优势学科偏向":["英语","生物"]}]')
    show_console = st.toggle("显示交互控制台（Prompt/Output/Issues）", value=True)
    # 新增：学术水平比例
    level_mix_text = st.text_input(
        "学术水平比例（高/中/低/差），如：高:0.25,中:0.25,低:0.25,差:0.25",
        value="高:0.25,中:0.25,低:0.25,差:0.25"
    )
    st.caption("比例作为采样先验写入白板，驱动四档分布；并联动下游维度避免‘清一色偏高’。")

with st.expander("说明", expanded=False):
    st.markdown("""
- **配额分桶调度**、**轻量过滤**、**SimHash 去同质化**；不过关即重采；  
- 自动落盘：`output/<run_id>/students_chunk_{i}.jsonl`；失败样本 `failures.jsonl`；  
- 学术水平四选一（固定文案）；代理名：姓 1–2 音节、名 1–3 音节，每节“拼音+1~5 声调”，下划线分隔。  
- **分布锚定**：侧边栏控制“高/中/低/差”比例；`agent_academic` 强制输出、`Validator` 兜底一致；  
- **乐观偏置抑制**：价值观/创造力/心理健康随锚自适应，轻量过滤中要求“中/较低/低”的**最小计数**（目标为“中/低/差”时生效）。  
""")

left, right = st.columns([1,3])
with left:
    n = st.number_input("生成数量（无限制）", min_value=1, value=100, step=1)
    rounds = st.number_input("最大协商轮数（无限制）", min_value=1, value=3, step=1)
    chunk_size = CHUNK_SIZE_DEFAULT
    start_btn = st.button("开始生成", type="primary")
    pause_btn = st.button("暂停生成 ⏸")
    resume_btn = st.button("继续生成 ▶️")

# ----------- 状态初始化 -----------
if "running" not in st.session_state: st.session_state.running = False
if "paused" not in st.session_state: st.session_state.paused = False
if "total_n" not in st.session_state: st.session_state.total_n = 0
if "max_rounds" not in st.session_state: st.session_state.max_rounds = 3
if "chunk_size" not in st.session_state: st.session_state.chunk_size = CHUNK_SIZE_DEFAULT
if "chunks_total" not in st.session_state: st.session_state.chunks_total = 0
if "chunk_idx" not in st.session_state: st.session_state.chunk_idx = 1
if "in_chunk_idx" not in st.session_state: st.session_state.in_chunk_idx = 0
if "global_idx" not in st.session_state: st.session_state.global_idx = 1
if "orch" not in st.session_state: st.session_state.orch = None
if "run_id" not in st.session_state: st.session_state.run_id = None
if "run_dir" not in st.session_state: st.session_state.run_dir = None
if "last_item" not in st.session_state: st.session_state.last_item = None
if "last_dialog" not in st.session_state: st.session_state.last_dialog = []
if "last_error" not in st.session_state: st.session_state.last_error = None
if "quota" not in st.session_state: st.session_state.quota = None
if "sim_gate" not in st.session_state: st.session_state.sim_gate = SimilarityGate(threshold=simhash_th)
if "level_mix" not in st.session_state: st.session_state.level_mix = _parse_level_mix(level_mix_text)

# ----------- 控制按钮 -----------
if start_btn:
    st.session_state.running = True
    st.session_state.paused = False
    st.session_state.total_n = int(n)
    st.session_state.max_rounds = int(rounds)
    st.session_state.chunk_size = int(chunk_size)
    st.session_state.chunks_total = math.ceil(st.session_state.total_n / st.session_state.chunk_size)
    st.session_state.run_id, st.session_state.run_dir = _init_run_dir()
    st.session_state.chunk_idx = 1
    st.session_state.in_chunk_idx = 0
    st.session_state.global_idx = 1
    st.session_state.orch = Orchestrator(max_rounds=st.session_state.max_rounds)
    st.session_state.last_item = None
    st.session_state.last_dialog = []
    st.session_state.last_error = None
    st.session_state.level_mix = _parse_level_mix(level_mix_text)
    st.session_state.quota = QuotaScheduler(
        st.session_state.total_n,
        user_quota_json=user_quota_json,
        level_mix=st.session_state.level_mix
    )
    st.session_state.sim_gate = SimilarityGate(threshold=simhash_th)
    st.success(f"输出目录：{st.session_state.run_dir}")
    _st_rerun()

if pause_btn and st.session_state.running:
    st.session_state.paused = True

if resume_btn and st.session_state.running:
    st.session_state.paused = False
    if st.session_state.run_dir:
        ck_idx, in_ck_idx, g_idx = _recover_progress_from_disk(st.session_state.run_dir, st.session_state.chunk_size)
        st.session_state.chunk_idx = ck_idx
        st.session_state.in_chunk_idx = in_ck_idx
        st.session_state.global_idx = g_idx
    _st_rerun()

# ----------- 进度条容器 -----------
chunk_prog_box = st.empty()
prog = st.empty()
status = st.empty()
preview_live = st.container()
console = st.container()
cards = st.container()

# ----------- 主循环 -----------
if st.session_state.running:
    chunk_prog = (st.session_state.chunk_idx-1) / max(1, st.session_state.chunks_total)
    chunk_prog_box.progress(
        min(chunk_prog, 1.0),
        text=f"分片进度：第 {st.session_state.chunk_idx}/{st.session_state.chunks_total} 片（每片 {st.session_state.chunk_size} 条） · 输出目录：{st.session_state.run_dir or '（未初始化）'}"
    )
    if st.session_state.paused:
        status.warning(f"已暂停（当前片已完成 {st.session_state.in_chunk_idx}/{st.session_state.chunk_size} 条；继续后自动续写）")
    else:
        current_chunk_total = min(st.session_state.chunk_size, st.session_state.total_n - (st.session_state.chunk_idx-1)*st.session_state.chunk_size)
        status.info(f"生成中：全局第 {st.session_state.global_idx}/{st.session_state.total_n} 条 · 当前片第 {st.session_state.in_chunk_idx+1}/{current_chunk_total} 条")

    current_chunk_total = min(st.session_state.chunk_size, st.session_state.total_n - (st.session_state.chunk_idx-1)*st.session_state.chunk_size)
    prog.progress(st.session_state.in_chunk_idx / max(1, current_chunk_total), text=f"当前片进度：{st.session_state.in_chunk_idx}/{current_chunk_total}")

    # 即时预览（本条）
    with preview_live:
        st.subheader("🖥️ 即时预览（本条生成的画像）")
        if st.session_state.last_error: st.error(st.session_state.last_error)
        if st.session_state.last_item:
            with st.expander(f"{st.session_state.last_item.get('姓名')} — {st.session_state.last_item.get('年级')} · 代理名：{st.session_state.last_item.get('代理名')}", expanded=True):
                st.json(st.session_state.last_item, expanded=False)

    # 交互控制台（本条）
    if show_console and st.session_state.last_dialog:
        with console:
            st.subheader("🖧 交互控制台（本条）")
            tabs = st.tabs(["学籍与发展阶段", "学业画像", "人格与价值观", "社交与创造力", "身心健康", "Validator", "Whiteboard RAW"])

            def _show_logs(agent_key: str):
                logs = [m for m in st.session_state.last_dialog if m["speaker"].startswith(agent_key)]
                if not logs:
                    st.info("暂无日志")
                else:
                    for m in logs[-12:]:
                        st.markdown(f"**{m['speaker']}**")
                        st.code(m["content"])

            with tabs[0]:
                _show_logs("学籍与发展阶段")
            with tabs[1]:
                _show_logs("学业画像")
            with tabs[2]:
                _show_logs("人格与价值观")
            with tabs[3]:
                _show_logs("社交与创造力")
            with tabs[4]:
                _show_logs("身心健康")
            with tabs[5]:
                _show_logs("Validator")
                import pandas as pd
                issues_rows = []
                for m in reversed(st.session_state.last_dialog):
                    if m["speaker"] == "Validator(issues)":
                        try:
                            arr = json.loads(m["content"])
                            if isinstance(arr, list):
                                issues_rows = arr; break
                        except: pass
                if issues_rows:
                    df = pd.DataFrame(issues_rows)
                    st.dataframe(df, use_container_width=True)
                else:
                    st.info("未捕获到结构化 issues。")
            with tabs[6]:
                for m in st.session_state.last_dialog[-40:]:
                    st.markdown(f"**{m['speaker']}**")
                    st.code(m["content"])

    # 片尾预览
    with cards:
        st.subheader("📂 当前片末尾预览（来自本地文件）")
        if st.session_state.run_dir:
            preview_items = _load_chunk_preview(st.session_state.run_dir, st.session_state.chunk_idx, max_items=6)
            if preview_items:
                start_idx = max(1, st.session_state.in_chunk_idx - len(preview_items) + 1)
                for idx, item in enumerate(preview_items, start=start_idx):
                    with st.expander(f"#{idx} — {item.get('姓名')}（{item.get('年级')}） · 代理名：{item.get('代理名')}", expanded=False):
                        st.json(item, expanded=False)
            else:
                st.info("当前片暂无已写入记录。")

    # 结束
    if st.session_state.global_idx > st.session_state.total_n or (st.session_state.quota and not st.session_state.quota.has_next()):
        prog.progress(1.0, text="当前片进度：完成 ✅")
        chunk_prog_box.progress(1.0, text="分片进度：全部完成 ✅")
        status.success(f"全部生成完成！文件已保存到：{st.session_state.run_dir}")
        st.session_state.running = False

    # 推进一个槽位（未暂停时）
    elif not st.session_state.paused:
        slot = st.session_state.quota.next_slot() if st.session_state.quota else {}
        target_sid = st.session_state.global_idx
        orch: Orchestrator = st.session_state.orch
        accepted = False
        error_trace = None

        for attempt in range(1, MAX_RETRIES_PER_SLOT+1):
            try:
                item, dialog = orch.run_one(target_sid, sampling_hint=slot)

                # 轻量过滤（含乐观偏置抑制）
                ok, reasons = _light_filter(item)
                if not ok:
                    error_trace = f"轻量过滤不通过：{'; '.join(reasons)}"
                    raise RuntimeError(error_trace)

                # 自相似度门控
                key_text = "｜".join([
                    str(item.get("年级","")), str(item.get("性别","")),
                    " ".join(item.get("人格",[]) if isinstance(item.get("人格"), list) else [str(item.get("人格",""))]),
                    str(item.get("价值观","")), str(item.get("社交关系","")),
                    str(item.get("创造力","")), str(item.get("心理健康",""))
                ])
                if st.session_state.sim_gate.too_similar(key_text):
                    error_trace = f"与已有样本过近（SimHash≤{st.session_state.sim_gate.threshold}）"
                    raise RuntimeError(error_trace)

                # 通过：写盘、登记相似度池、缓存界面
                _append_record(st.session_state.run_dir, st.session_state.chunk_idx, item)
                st.session_state.sim_gate.accept(key_text)

                st.session_state.in_chunk_idx += 1
                st.session_state.global_idx += 1
                st.session_state.last_item = item
                st.session_state.last_dialog = dialog
                st.session_state.last_error = None

                if st.session_state.in_chunk_idx >= st.session_state.chunk_size:
                    st.session_state.in_chunk_idx = 0
                    st.session_state.chunk_idx += 1

                accepted = True
                break

            except Exception as e:
                err_msg = f"槽位{slot} · 尝试{attempt}/{MAX_RETRIES_PER_SLOT}失败：{e}"
                st.session_state.last_error = err_msg
                if attempt == MAX_RETRIES_PER_SLOT:
                    _append_failure(st.session_state.run_dir, {"slot": slot, "sid": target_sid, "error": str(e)})
                    st.session_state.global_idx += 1

        if not accepted and error_trace:
            st.error(error_trace)

        _st_rerun()
