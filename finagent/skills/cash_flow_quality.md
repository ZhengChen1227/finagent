---
name: cash_flow_quality
title: 现金流质量分析
triggers: [现金含量, 经营现金流, 利润与现金流, 现金流背离, 现金转化]
tools: [get_indicator, compute_growth, search_disclosure, check_articulation]
priority: 高
---

# 现金流质量分析

## 适用场景

净利润与经营活动现金流量净额出现明显差距时启用。

## 执行步骤

1. 用 `get_indicator` 取当期 `net_profit`、`netcash_operate`、`depr_fa`、
   `amort_ia`、`amort_lpe`、`amort_rou`、`impairment_provision`。
2. 用 `get_indicator` 取 `cash_conversion_naive` 与 `cash_conversion_adjusted`。
3. 若两者差异显著，用 `get_indicator` 取 `dep_to_revenue` 判断是否为重资产结构。
4. 若调整后仍显著偏离 1，用 `search_disclosure` 检索「经营性应收项目」
   「存货」及「应收账款账龄」定位营运资本变动来源。

## 判读规则

| 情形 | 判读 | 类别 |
|---|---|---|
| 朴素比值高，调整后接近 1，折旧占收入 >15% | 重资产结构性特征 | 结构性特征 |
| 朴素与调整后均接近 1 | 现金转化正常 | 无需报告 |
| 调整后显著 < 0.5 | 收入未转化现金，营运资本占用或确认问题 | 异常信号 |
| 调整后显著 > 1.8 | 现金流入超出经营成果，需查预收与应付异常 | 异常信号 |

## 常见误判（务必避免）

**把重资产企业的"现金流远高于利润"当作利润质量问题。**

折旧摊销是非付现成本，一年可达数百亿，会把朴素现金含量推到 8 倍以上。
面板、钢铁、化工、电力等重资产行业普遍如此，这是会计准则的结果，不是造假迹象。
判断前必须先看折旧摊销占收入比重。

## 输出要求

必须同时给出朴素口径与调整后口径，并说明差额来自哪些项目。
只给一个口径等于误导读者。
