---
name: nonrecurring_items
title: 非经常性损益与利润质量识别
triggers: [非经常性损益, 扣非, 政府补助, 利润质量, 投资收益, 资产处置]
tools: [get_indicator, search_disclosure, read_page]
priority: 中
---

# 非经常性损益与利润质量识别

## 适用场景

评估报表利润的可持续性，判断业绩对非经常项目的依赖程度。

## 执行步骤

1. 用 `get_indicator` 取 `parent_net_profit` 与 `deduct_parent_net_profit`。
2. 差值即非经常性损益；用 `get_indicator` 取 `nonrecurring_ratio` 看占比。
3. 用 `search_disclosure` 检索「非经常性损益明细表」，逐项拆分构成。
4. 用 `read_page` 读取该页，核对政府补助、资产处置、公允价值变动等具体金额。
5. 用 `get_indicator` 取 `other_income`（其他收益，主要为政府补助）
   与 `invest_income`，判断其绝对额与同比。

## 判读规则

| 非经常性损益占归母净利 | 判读 |
|---|---|
| < 10% | 利润质量良好 |
| 10% ~ 30% | 需关注，拆分构成 |
| > 30% | 利润对非经常项目依赖较高，须重点提示 |
| 归母净利为负 | 比率不适用，改为看非经常项目的绝对额方向 |

## 注意事项

- 归母净利润为负时，非经常性损益占比失去意义，不得机械套用阈值。
- 政府补助若为持续性、与日常经营相关，需判断其是否具备可持续性，
  不能一概视为"一次性"。
- 报告须区分「报表利润」与「扣非利润」两个口径，并说明差额来源。
