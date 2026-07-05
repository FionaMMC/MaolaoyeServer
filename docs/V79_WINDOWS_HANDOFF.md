# Windows 端：v7.9 小市值策略每周篮子生成 + 上传（一键 handoff）

> **目标**：让 **Windows 单机**（有全套新鲜数据的那台）每周产出 v7.9 的**可执行目标篮子**，上传到云服务器，server 端 relay 适配器接住并（dry-run 阶段）落 diff、（上线后）下单。**全程不需要 Mac。**
> **读者**：接手运行的同伴。跟着走即可，`【CONFIRM】` 标记处需你按本机实际确认一次。
> **类比**：与 `WINDOWS_PRED_REFRESH_HANDOFF.md`（v20h pred 刷新）同构——那个推 pred 5 件套，这个推 1 个 basket 文件。

---

## 0. 为什么在 Windows 跑

v7.9 需要**全市场个股行情 + PIT 基本面(ROE/股本) + 同花顺行业日频 + 9 只 ETF**，且信号（phase8/phase12 CSV）目前就是在**这台 Windows** 上算到最新交易日的。Mac 端这些数据冻在 2026-04-30，无法产出"当周"篮子。所以 producer 必须跑在数据新鲜的这台。

## 1. 前提（一次性确认）

- [ ] **代码在位**：`small_cap_peer` repo（含 `round4/v7.9/`、`round4/v7.6/`、`common.py`），Hydra `permenant_portfolio/v49`。
- [ ] **数据新鲜**：`round4/v7.6/baseline.py` 能跑通（个股缓存、`ths_industry` 行业日频、v49 的 9 只 ETF parquet 都到最近交易日）。跑一次 `python round4/v7.9/baseline.py` 若能刷出 `output/phase12_size_tail_state_predictions.csv` 到最新日期即 OK。
- [ ] **Python 环境**：small_cap_peer 的 venv（pandas/numpy/scikit-learn；phase12 用 sklearn `LogisticRegression`，无需 torch）。
- [ ] **环境变量**：`STRATEGY_DATA` 指向数据根（`common._find_data_root` 需要它，否则从任意 cwd 跑可能找不到根）。【CONFIRM】设成你本机的 `...\strategy_search\data` 或对应根。
- [ ] **服务器凭据**：`QMT_PIPELINE_BASE_URL=http://120.26.138.82:8000`、`QMT_PIPELINE_API_KEY=pipeline-v23-shared-secret-2026`。

## 2. 一次性：确认 14 行业 → 可执行 ETF 映射（B3）

v7.9 的防御分支之一 "TOP2" = 原策略在 `round4/v7.6/baseline.py:54-93` 定义的 **14 个大板块里按 52 周动量最强的 2 个**（动量加权）。原回测里它是**板块指数收益**（各板块子行业指数收盘的等权均值），不落地到具体标的。上实盘要把每个选中板块映射成一只**可交易的板块 ETF**（这才是"持有该板块指数"的忠实实现）。

下表是 **14 个固定板块 → 建议 ETF**。板块名与 `GROUPS` 的 key 一一对应。**【CONFIRM】上线前逐只核对代码是否现存、日均成交是否够（微盘策略防御腿，宁可选最流动的宽基板块 ETF）。** 这张表就是 producer 里的 `SECTOR_ETF` 配置，改这里即可。

| GROUP（repo 板块名） | 建议 ETF | QMT code | 备注 |
|---|---|---|---|
| 电子 | 电子ETF | `515260.SH` | 或半导体 `512480.SH`（更窄更活） |
| 计算机 | 计算机ETF | `512720.SH` | |
| 通信 | 通信ETF | `515880.SH` | |
| 有色金属 | 有色金属ETF | `512400.SH` | |
| 电力设备 | 新能源ETF | `516160.SH` | 电机/风光/电池，新能源宽基最贴 |
| 银行 | 银行ETF | `512800.SH` | |
| 非银金融 | 证券ETF | `512880.SH` | 非银主要是券商；保险占比小 |
| 食品饮料 | 食品饮料ETF | `515170.SH` | 或酒 `512690.SH` |
| 医药生物 | 医药ETF | `512010.SH` | |
| 能源 | 煤炭ETF | `515220.SH` | 能源=煤炭+石油，煤炭为主；石油可选 `561360.SH` |
| 汽车 | 汽车ETF | `516110.SH` | |
| 国防军工 | 军工ETF | `512660.SH` | |
| 机械设备 | 机械/通用设备 ETF | `【CONFIRM】` | ⚠️ 机械 ETF 偏薄，核对最活的一只；缺则见 §5 fallback |
| 基础化工 | 化工ETF | `516020.SH` | 或化工龙头 `159870.SZ` |

> ⚠️ 只有当周 TOP2 命中的两个板块才会用到对应 ETF。**机械设备**这类薄 ETF 若确无合适标的，见 §5 fallback（该板块当周退回 Hydra 防御腿）。

## 3. Producer 脚本 `produce_v79_basket.py`

放在 `small_cap_peer/` 根。它复用 v7.9 全套逻辑（不改策略），只是把**当周**的目标拍平成一个可执行 `{code: weight}`。

```python
"""produce_v79_basket.py — v7.9 当周可执行篮子 → parquet → 上传 server。
用法: python produce_v79_basket.py [--date YYYYMMDD] [--skip-upload]
不传 --date 用最新交易周。"""
import os, sys, runpy, argparse
from pathlib import Path
import numpy as np, pandas as pd, requests

ROOT = Path(__file__).resolve().parent                    # small_cap_peer/
V76  = ROOT / "round4" / "v7.6" / "baseline.py"
PH14 = ROOT / "round4" / "v7.9" / "ml_switch_phase14_aux_only_state_overlay.py"
V49  = Path(r"【CONFIRM】...\permenant_portfolio\v49")     # Hydra v49 目录
BASE = os.environ.get("QMT_PIPELINE_BASE_URL", "http://120.26.138.82:8000")
KEY  = os.environ.get("QMT_PIPELINE_API_KEY", "pipeline-v23-shared-secret-2026")
CASH_BUFFER = 0.01

# 14 板块 → ETF（§2 的表；核对后填全）
SECTOR_ETF = {
    "电子":"515260.SH","计算机":"512720.SH","通信":"515880.SH","有色金属":"512400.SH",
    "电力设备":"516160.SH","银行":"512800.SH","非银金融":"512880.SH","食品饮料":"515170.SH",
    "医药生物":"512010.SH","能源":"515220.SH","汽车":"516110.SH","国防军工":"512660.SH",
    "机械设备":"【CONFIRM】","基础化工":"516020.SH",
}

def hydra_v49_weights(target_ts):
    """调 v49 compute_baseline 取当期 ETF 权重 {etf: w} sum~1。"""
    sys.path.insert(0, str(V49)); sys.path.insert(0, str(V49.parent))
    import config as cfg                                   # v49/config.py
    from weight_methods import compute_baseline           # v49/weight_methods.py
    from data.loader import load_extended_close_px        # 【CONFIRM】v49 loader 的 DATA_DIR 需指向本机新鲜 ETF 目录
    ext = load_extended_close_px(etf_codes=cfg.ETF_CODES)
    rebal = [d for d in ext.index if d <= target_ts]
    asset_df, _, _ = compute_baseline(ext, rebal, cfg.ETF_CODES, cfg.QUADRANT_MAP, method="inv_vol")
    return {k: float(v) for k, v in asset_df.iloc[-1].to_dict().items() if v and v > 0}

def top2_sector_etf_weights(g, d1):
    """当周 TOP2 板块 → 映射 ETF，动量加权（复刻 phase8: mm.iloc[:2] + max(mm,0.01) 归一）。"""
    mom52 = g["mom52"]; we = pd.DatetimeIndex(g["we"])
    pw = we[we < d1][-1]
    mm = mom52.loc[pw].dropna().sort_values(ascending=False)
    top2 = list(mm.index[:2])
    w = np.array([max(mm[s], 0.01) for s in top2], float); w = w / w.sum()
    out = {}
    for s, wi in zip(top2, w):
        etf = SECTOR_ETF.get(s)
        if not etf or etf.startswith("【"):                # §5 fallback：无 ETF 的板块权重退回 Hydra
            return None                                    # 交给调用方走 Hydra 防御
        out[etf] = out.get(etf, 0.0) + float(wi)
    return out

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--date"); ap.add_argument("--skip-upload", action="store_true")
    a = ap.parse_args()
    g = runpy.run_path(str(V76)); H = runpy.run_path(str(PH14))   # 载入全部数据 + helpers
    we = pd.DatetimeIndex(g["we"])
    d1 = we[-1] if not a.date else we[we <= pd.Timestamp(a.date)][-1]
    d0 = we[we < d1][-1]
    codes = g["codes"]

    # 1. 本月 TOP50（沿用上月末篮子，见 baseline.py:70-74）
    pos_month = (pd.Timestamp(d0.replace(day=1)) - pd.offsets.MonthEnd(1)).strftime("%Y-%m")
    cand = [d for d in g["month_ends"] if str(d)[:7] == pos_month]
    sel, w = H["select_top50_pos"](g, cand[-1] if cand else d0)
    top50 = {codes[i]: float(wi) for i, wi in zip(sel, w)}         # {stock: 1/N}

    # 2. 当周 gate（当月 gate + 上周 state）→ sleeve + 权益比例
    gate = H["compute_monthly_gates"](g).get(d1.strftime("%Y-%m"), {"t1":False,"aux":False,"narrow":False})
    st = H["load_state_signal"]("logistic", "choose_hard_rot55_risk40").get(d0, {"active":False})
    hydra = hydra_v49_weights(d1)
    if gate["t1"]:
        sleeve, basket = "T1_5050", _blend({c:0.5*x for c,x in top50.items()}, {c:0.5*x for c,x in hydra.items()})
    elif gate["aux"] and st.get("active"):
        top2 = top2_sector_etf_weights(g, d1)
        sleeve, basket = ("AuxState_TOP2", top2) if top2 else ("AuxState_TOP2_fallback_Hydra", hydra)
    elif gate["aux"]:
        sleeve, basket = "Aux_Hydra", hydra
    else:
        sleeve, basket = "TOP50", top50

    # 3. 归一到 1-CASH_BUFFER，写 parquet
    s = sum(basket.values()); basket = {c: v/s*(1-CASH_BUFFER) for c, v in basket.items()}
    dd = d1.strftime("%Y%m%d")
    df = pd.DataFrame([{"code":c,"weight":round(w,6),"sleeve":sleeve,"decision_date":dd} for c,w in basket.items()])
    out = ROOT / f"v79_target_{dd}.parquet"; df.to_parquet(out, index=False)
    latest = ROOT / "v79_target_latest.parquet"; df.to_parquet(latest, index=False)
    print(f"[v79] {dd} sleeve={sleeve} n={len(df)} -> {out}")
    print(df.to_string(index=False))

    if not a.skip_upload:
        with open(latest, "rb") as f:
            r = requests.post(f"{BASE}/admin/upload-data",
                              params={"strategy_name":"v79","filename":"v79_target_latest.parquet"},
                              headers={"Authorization":f"Bearer {KEY}"},
                              files={"files":("v79_target_latest.parquet", f)})
        print("upload:", r.status_code, r.text[:200])

def _blend(*dicts):
    out={}
    for d in dicts:
        for c,x in d.items(): out[c]=out.get(c,0.0)+x
    return out

if __name__ == "__main__":
    main()
```

> **注意**：上传的 `filename` / `strategy_name=v79` 决定文件落到 server 的 `plugins/v79/data/v79_target_latest.parquet`（在 relay 的 `data_files` 白名单内）。【CONFIRM】首次上传前，先跟 server 端确认 v79 已注册（`GET /admin/...` 或问服务器侧）。

## 4. 一键运行 `run_v79.bat`

```bat
@echo off
cd /d %~dp0
set STRATEGY_DATA=【CONFIRM】...\strategy_search\data
set QMT_PIPELINE_BASE_URL=http://120.26.138.82:8000
set QMT_PIPELINE_API_KEY=pipeline-v23-shared-secret-2026
.\venv\Scripts\python produce_v79_basket.py %*
```

- 先验证不上传：`run_v79.bat --skip-upload` → 看打印的 sleeve + 篮子（权重和≈0.99），核对 TOP50 是不是当月 50 只、防御周是不是 ETF。
- 正式：`run_v79.bat` → 产出 + 上传。
- 每周节奏：**每周固定一天收盘后**跑一次（与 v20h 周更同日即可）。可挂 Windows 任务计划程序每周自动跑。

## 5. 已知点 / fallback

- **B4（Hydra 数据源）**：`v49/data/loader.py` 里 `DATA_DIR` 是硬编码 Windows 路径。【CONFIRM】指向本机新鲜的 9 只 ETF parquet 目录（`510300/159915/511260/518880/159981/159985/159930/513500/513100` + 早期桥接 `511010`）。
- **机械设备无 ETF fallback**：`top2_sector_etf_weights` 若命中的板块在 `SECTOR_ETF` 里没配可交易 ETF，函数返回 `None` → 当周整体退回 **Hydra 防御腿**（保守、可执行）。等确认好机械 ETF 再补进表即可。
- **数据必须新鲜**：篮子的 `decision_date` = 你跑的那个交易周末。server 端 relay 用它做幂等（同一 decision_date 重复上传不会重复下单）。所以**不要拿旧数据跑**，否则 server 会当成"还是上周的篮子"跳过。
- **dry-run 阶段**：server 现在是 dry_run，relay 只 log 不下单。上线（dry_run:false）由 server 侧在**多租户台账地基验证通过后**切，跟你无关——你照常每周推篮子即可。

## 6. 验收（首次）

1. `run_v79.bat --skip-upload` → 篮子打印正常（TOP50 周应有 ~50 只 6 位股票码；防御周应是若干 ETF）。
2. 去掉 `--skip-upload` 上传 → `upload: 200`。
3. 找 server 侧确认：`plugins/v79/data/v79_target_latest.parquet` 已更新，且当日 pipeline 日志有 `V79 DRY-RUN decision_date=... target=N`。
4. 之后每周重复第 2 步即可。
