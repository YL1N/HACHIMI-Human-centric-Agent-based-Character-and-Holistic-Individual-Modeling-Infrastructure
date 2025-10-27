# 学生画像 · 批量后验评估（JSONL 分片）

## 一页结论

- 样本量：96
- Gate 通过率：
  - ok_schema: 1.0000
  - ok_dev3: 1.0000
  - ok_grade_age: 1.0000
  - ok_subjects: 1.0000
  - ok_agentname: 0.2083
  - ok_academic_level: 1.0000
  - ok_style_values: 1.0000
  - ok_style_social: 1.0000
  - ok_style_creativity: 1.0000
  - ok_style_psych: 1.0000
  - ok_sensitive: 1.0000
  - ok_values_7dims: 1.0000
  - ok_crea_8dims_radar: 0.9167
  - ok_psy_coverage: 0.4896
- 近重复率（阈=10比特）：0.0000；最大簇：1
- 学科组合唯一数：60；Top10 占比：0.4687

## 失败样例（Top-K id）

- ok_agentname: [1, 2, 3, 4, 6, 7, 8, 9]
- ok_crea_8dims_radar: [5, 11, 16, 30, 57, 75, 83, 84]
- ok_psy_coverage: [1, 4, 7, 8, 11, 14, 15, 16]

## 分布概览

{
  "count": 96,
  "grade_dist": {
    "初二": 0.3229166666666667,
    "初一": 0.125,
    "高一": 0.34375,
    "高二": 0.03125,
    "四年级": 0.020833333333333332,
    "三年级": 0.010416666666666666,
    "六年级": 0.125,
    "五年级": 0.020833333333333332
  },
  "gender_dist": {
    "女": 0.53125,
    "男": 0.46875
  },
  "academic_dist": {
    "中：成绩全校排名前10%至30%": 0.96875,
    "高：成绩全校排名前10%": 0.03125
  }
}

## 指标说明
- ok_* 为 Gate 合规；distinct-* 为字gram多样性；crea_std 为创造力八维等级起伏（std）；coh_values_psy 为“价值观·身心健康 vs 心理综合状况”的差值（越小越一致）。
