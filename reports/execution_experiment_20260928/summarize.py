"""Produce reproducible tables and exploratory paired-month return intervals."""
from pathlib import Path
import json
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
LABELS = {
    'fixed50_1d':'固定0.5%，一天（辅助对照）',
    'fixed50_3d':'固定0.5%，三天',
    'rolling50_focus_buy_3d':'三只买入逐日更新参考价+0.5%，三天',
    'focus_buy100_3d':'三只买入固定1%，三天',
    'gold100_us150_3d':'黄金买入固定1%、纳指/标普1.5%，三天',
}


def primary(frame):
    return frame[(frame.capital==200000)&(frame.slip_bps==5)&frame.adjacent&frame.touch&~frame.reverse_symbols]


def main():
    metrics = pd.read_csv(HERE/'execution_metrics.csv')
    portfolios = pd.read_csv(HERE/'portfolio_results.csv')
    episodes = pd.read_csv(HERE/'matched_episodes.csv')
    nav = pd.read_csv(HERE/'primary_nav.csv',index_col=0,parse_dates=True)
    monthly = nav.resample('ME').last()
    logs = np.log(monthly/monthly.shift().fillna(200000.))
    rng = np.random.default_rng(20260928)
    n = len(logs)
    starts = rng.integers(0,n,size=(5000,(n+2)//3))
    indices = ((starts[:,:,None]+np.arange(3))%n).reshape(5000,-1)[:,:n]
    base = logs['fixed50_3d'].to_numpy()
    bootstrap = {}
    for policy in nav.columns:
        x = logs[policy].to_numpy()
        delta = np.exp(x[indices].sum(axis=1))-np.exp(base[indices].sum(axis=1))
        bootstrap[policy] = dict(observed_total_return_delta_pp=float((nav[policy].iloc[-1]-nav['fixed50_3d'].iloc[-1])/200000*100),
            paired_month_block3_ci95_pp=(np.quantile(delta,[.025,.975])*100).tolist(),months=n,draws=5000)
    (HERE/'return_bootstrap.json').write_text(json.dumps(bootstrap,indent=2))
    lines = ['# 黄金、纳指、标普：执行政策对照实验（2026-09-28）',
        '', '## 结论', '',
        '在本次同订单日线模型中，差异化固定上限方案最有利于首日完成和减少等待；逐日更新参考价也显著提高模型完成度。二者组合累计净收益都略高于固定0.5%/三天，但收益改善很小，配对月度重采样区间含零，不能认定收益优势已被证明。优先候选是黄金买入1%、纳指/标普买入1.5%，不是无条件实盘推荐。未修改或部署线上策略。',
        '', '## 设计与口径', '',
        '- 主样本：2019-12-05—2026-09-03，原Hydra月度权重；共同81次目标，最后目标日2026-07-31。初始化保留在NAV中，但排除在成交统计中，剩余80轮。',
        '- 主场景：20万元，5bp单边不利滑点，1bp佣金且每次模拟成交最低5元，100份整手，单标的每日容量上限为实际全天成交量的1%。分红、应收分红、到账及拆分单独处理。最低5元佣金是沿用的模型假设，尚未重新核验实际券商费率；开盘/盘中拆分模拟成交分别计费可能高估同一券商委托的佣金。',
        '- 主场景按线上日期约束：参考日和执行日须相邻自然日；月末遇周末等情况，等待首个合格交易对，以该参考日已知收盘价和NAV计算目标数量。窗口从首次执行日起算三个交易日，跳过的日期仍占窗口，不延期为三次实际提交。',
        '- 新政策仅改变黄金518880、纳指513100、标普513500的买入限价/参考价；其他六只以及全部卖单维持原始锚点0.5%。一天辅助对照则把整个组合窗口改为一天。所有补单只处理剩余份额。',
        '- 固定方案整轮不重置买入上限；滚动方案只使用执行日前一交易日已知收盘价，不能用当日收盘价。滚动0.5%不是相对最初参考价的累计0.5%保护。',
        '- 同订单比较：从固定0.5%/三天的完整组合轨迹提取每轮起始持仓、可用现金、分红权利和目标订单，克隆给每种政策。各政策计划交易金额分母逐项相同；主场景三只共82笔买入需求（黄金28、纳指28、标普26）。每轮比较三天，互不串联。',
        '- 连续组合比较：每种政策自行延续持仓、现金和净值，下一轮按相同权重计算需求，因而订单分母可能不同。此层用于收益，不以其成交率冒充同订单结果。',
        '- 完成度均为参考价格折算的金额完成度，允许部分成交，不是订单笔数完成率。',
        '', '## 同订单的买入完成度（主场景）', '',
        '|政策|黄金|纳指|标普|三只合计|三只首日完成度|',
        '|---|---:|---:|---:|---:|---:|']
    pm = primary(metrics)
    matched = pm[(pm['mode']=='matched')&(pm.side=='BUY')]
    for policy,label in LABELS.items():
        d = matched[matched.policy==policy].set_index('group')
        values = [d.loc[s,'completion']*100 for s in ['518880.SH','513100.SH','513500.SH','focus3']]
        lines.append('|'+label+'|'+'|'.join(f'{v:.2f}%' for v in values)+f"|{d.loc['focus3','first_day_completion']*100:.2f}%|")
    lines += ['', '## 成交成本和等待（主场景）', '',
        '成交价偏移=(成交价−首轮参考价)按成交份额加权，正值表示买贵；加费用列再加模拟佣金。仅统计已成交部分，存在选择偏差，不能单凭低偏移判断限价更优。延迟也只针对已成交部分；未成交部分没有被当作零延迟。', '',
        '|政策|已成交买入价偏移|含模拟佣金|已成交金额加权延迟（交易日）|三日平均三只欠配占组合NAV|',
        '|---|---:|---:|---:|---:|']
    pe = primary(episodes).groupby('policy').mean(numeric_only=True)
    for policy,label in LABELS.items():
        d = matched[(matched.policy==policy)&(matched.group=='focus3')].iloc[0]
        lines.append(f"|{label}|{d.conditional_price_cost_bps:.2f}bp|{d.conditional_price_and_fee_bps:.2f}bp|{d.conditional_delay_sessions:.3f}|{pe.loc[policy,'focus_underweight']*100:.3f}%|")
    lines += ['', '完整三天机会成本比较还把未成交部分纳入：构造在同一参考价立即调到目标、无费用但受同一可用现金约束的虚拟组合，第三日其NAV减实际NAV定义为终点执行损失。应收未到账分红不能用于虚拟交易，资金不足时同比例缩减虚拟买入。它是全九只的组合指标，不等于上表的已成交买入价差。', '',
        '|政策|每轮平均第三日执行损失（元）|', '|---|---:|']
    for policy,label in LABELS.items():
        lines.append(f"|{label}|{pe.loc[policy,'terminal_shortfall']:.2f}|")
    lines += ['', '滚动补单虽然提高完成度，但第三日平均执行损失略高；差异化上限与基线的第三日损失接近。不能把补单后的上涨归为无成本。',
        '', '## 连续组合收益（主场景，已扣模拟费用）', '',
        '|政策|全期累计净收益|2025年以来净收益|全期最大回撤|相对基线累计收益差|',
        '|---|---:|---:|---:|---:|---:|']
    pr = primary(portfolios).set_index('policy')
    for policy,label in LABELS.items():
        r = pr.loc[policy]
        delta = (r.total_return-pr.loc['fixed50_3d','total_return'])*100
        lines.append(f'|{label}|{r.total_return*100:.2f}%|{r.return_since_2025*100:.2f}%|{r.max_drawdown*100:.2f}%|{delta:+.3f}个百分点|')
    lines += ['', '差异化方案相对基线累计收益增加0.1356个百分点，在20万元初始本金下约271.20元，是近七年的累计差额，不是年化提升。滚动方案增加0.1284个百分点，约256.80元。',
        '', '配对月度收益采用3个月循环块bootstrap，共82个月、5,000次重采样。以下为累计收益差的探索性95%区间；历史数据已用于参数选择，不是严格样本外验证。', '',
        '|政策|累计收益差95%区间（百分点）|', '|---|---:|']
    for policy in ['rolling50_focus_buy_3d','focus_buy100_3d','gold100_us150_3d']:
        lo,hi = bootstrap[policy]['paired_month_block3_ci95_pp']
        lines.append(f'|{LABELS[policy]}|[{lo:.3f}, {hi:.3f}]|')
    lines += ['', '## 压力测试', '',
        '20万/100万元 × 5/25/50bp滑点 × 相邻日期/全部交易日两种模式 × 五个政策，共60次组合回放。再加主本金下仅开盘成交、反转标的资金分配顺序两项，各五个政策，合计70次组合回放、5,600次同订单三日片段回放。', '',
        '|本金|滑点|固定0.5%完成度|滚动0.5%完成度|差异化上限完成度|差异化累计收益增量|',
        '|---|---:|---:|---:|---:|---:|']
    stress = metrics[(metrics['mode']=='matched')&(metrics.group=='focus3')&(metrics.side=='BUY')&metrics.adjacent&metrics.touch&~metrics.reverse_symbols]
    stress_return = portfolios[portfolios.adjacent&portfolios.touch&~portfolios.reverse_symbols]
    for (capital,slip),d in stress.groupby(['capital','slip_bps']):
        d = d.set_index('policy')
        r = stress_return[(stress_return.capital==capital)&(stress_return.slip_bps==slip)].set_index('policy')
        delta = (r.loc['gold100_us150_3d','total_return']-r.loc['fixed50_3d','total_return'])*100
        lines.append(f"|{capital/10000:.0f}万|{slip:.0f}bp|{d.loc['fixed50_3d','completion']*100:.2f}%|{d.loc['rolling50_focus_buy_3d','completion']*100:.2f}%|{d.loc['gold100_us150_3d','completion']*100:.2f}%|{delta:+.3f}个百分点|")
    lines += ['', '仅开盘成交是另一个执行场景，不是组合收益的数学下界；放弃亏损订单有时会提高回测收益。在该场景下，滚动与差异化方案仍提高相对于固定0.5%的完成度并略提高累计收益。反转标的处理顺序时，主场景三个三日候选的组合收益不变。',
        '', '## 与前两次结果的衔接', '',
        '之前82.58%是全九只、买卖合并、包含统一初始化，并允许每个交易日执行的指标。本次主场景加入相邻自然日约束，因此它的同口径全组合数变成73.50%；本报告用于方案比较的86.04%则是三只买入、排除初始化、同订单样本。两者不能串成一条“成交率增长”结论。',
        '', '上一次排除初始化后三只买入71.37%也来自全部交易日的原参考日。新主场景会等待合格日期，并按当时已知收盘价重算首次目标和限价，所以黄金92.73%、纳指69.07%、标普76.18%不等于旧参考日结果，不能把日期重排的效果归为限价放宽效果。所有候选在本次主场景使用同一日期安排。',
        '', '旧9月6日报告的滚动补单是最多三次合格执行机会，本次统一最多三个交易日。主样本三天平均只有2.675个合格执行日。本次不复现旧报告的近99%结果，也不把它当作生产保证。',
        '', '## 实务结论与限制', '',
        '如果优先减少首日仓位偏离，在本次候选中优先研究“黄金买入上限1%，纳指/标普1.5%，固定整轮上限，最长三交易日”；其他六只和卖单维持0.5%。三只统一1%对纳指覆盖不足；每日更新参考价能补上更多纳指订单，但在本样本买入价格偏移和等待更大。',
        '', '这仍只是一项日线候选研究：严格穿过限价才模拟盘中成交，但无法观察14:55撤单前是否已触价、成交队列、盘口深度、IOPV/估计净值和真实溢价。全天最低/最高价可能出现在撤单后；当日成交量上限也是事后信息。盘中先卖后买的实际时间顺序仍未知。必须用分钟或真实订单数据验证后才能估计实盘完成度。',
        '', '未建模全部线上风险门禁、行情到达/券商拒单/停机与审核状态。本次测试日期规则不等于完整生产链重放。缺少更细数据时，不能声称放宽后的高完成度足以抵消异常溢价风险。',
        '', '## 验证和复现', '',
        '- 六个合成边界测试：滚动仅使用前收盘且不改卖单/其他ETF、跨周末不延长窗口、部分成交容量不重复、盘中卖出不能倒填开盘买入、分红与拆分、未到账分红不可用。',
        '- 六个全部交易日基线（本金×滑点）与原回测逐日NAV及金额完成度一致。每组同订单比较逐项核验分母相同。记录原始输入与模型SHA-256于validation.json。',
        '- 运行：`python3 reports/execution_experiment_20260928/compare_policies.py`；`python3 reports/execution_experiment_20260928/summarize.py`。',
        '- 明细：execution_metrics.csv、portfolio_results.csv、matched_episodes.csv、primary_nav.csv、return_bootstrap.json、validation.json。']
    (HERE/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print('Wrote REPORT.md and return_bootstrap.json')


if __name__ == '__main__':
    main()
