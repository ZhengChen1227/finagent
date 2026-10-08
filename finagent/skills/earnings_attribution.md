---
name: earnings_attribution
title: 业绩变动归因
triggers: [业绩变化, 同比, 环比, 亏损, 盈利下滑, 业绩原因]
tools: [compute_growth, get_indicator, run_anomaly_rules, search_disclosure]
priority: 高
---

# 业绩变动归因

## 适用场景

收入或利润出现显著同比、环比变动时启用。

## 执行步骤

1. 用 `compute_growth` 分别取收入与归母净利润的累计同比、单季度同比、单季度环比。
2. 判断变动是**量**还是**价**驱动：检索「销售价格」「销售数量」「销量」等经营数据。
3. 拆分利润驱动项：用 `get_indicator` 取 `gross_margin`、`operating_cost`、
   `sale_expense`、`manage_expense`、`research_expense`、`finance_expense`。
4. 若为周期性行业，检索报告期内的行业供需描述。

## 判读规则

- **必须区分季节性**：周期性行业（生猪养殖、面板、建材等）的季节性规律主导环比，
  此时同比比环比更具解释力，报告须明示这一点。
- **累计同比与单季度同比可能背离**：若累计同比为正而单季度同比为负，
  说明业绩拐点已现，这是比单看累计同比更重要的信息。
- **单季度环比必须基于还原后的单季度值**，不得用累计数相除。

## 输出要求

给出「本期 vs 上期」的量化对比，并说明变动的驱动项及其证据来源。
不得仅描述"业绩下滑"而不给出量化拆解。
