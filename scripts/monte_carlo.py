"""Monte Carlo 分布推定 — 点推定を分布に置き換える（検証強化 V-4）.

正準バックテスト（gap-aware・¥600k・noTP/ATR3.0/TS5）のトレード列を
ブートストラップ再抽出して年間パスを数千回シミュレーションし、
「+0.35%/月」という点推定では見えない意思決定量を出す:

- CB（純損失 ¥90,000）が 1 年以内に発動する確率
- 年間損益の分布（5/25/50/75/95 パーセンタイル）、P(黒字)、P(年 ¥100k 目標達成)
- 年間最大ドローダウンの分布

シナリオ:
- full_4y: 2022-2025 の全トレード（全レジーム混合。2022-23 の逆風年を含む）
- recent: 2024-01〜2026-06 のトレード（現行レジーム継続を仮定した楽観側）
それぞれ iid ブートストラップと循環ブロック・ブートストラップ（クラスタ保存）で実行。

多重検定割引（F-3 の定量版）:
グリッド探索の反復（96〜180 combos × 複数ラウンド）で「たまたま良く見える」
パラメータを選んだ分のかさ上げを Deflated Sharpe Ratio（Bailey & López de Prado
2014）で割り引く。DSR = 「観測 Sharpe が、K 回の試行から最良を選んだ場合に
期待されるノイズ最大値を超えている確率」。試行 Sharpe の分散は
.backtest_cache/grid_search_results.json（96 combos の実測）から推定する。

制約（レポートに明記）:
- トレード列を逐次独立とみなす（実際は最大2銘柄同時保有の重なりあり）
- ブロック長 20 は数週間規模のクラスタは保存するが、年単位のレジーム持続は
  保存しない（レジーム条件付けは recent シナリオが担当）

Usage:
    python scripts/monte_carlo.py                     # 5000 パス・既定シナリオ
    python scripts/monte_carlo.py --sims 10000 --seed 7
    python scripts/monte_carlo.py --json docs/backtests/montecarlo_YYYY-MM-DD.json
"""
import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

import numpy as np  # noqa: E402

_EULER_GAMMA = 0.5772156649015329
_NORM = NormalDist()

CB_THRESHOLD_JPY = 90_000.0
TARGET_ANNUAL_JPY = 100_000.0
INITIAL_CAPITAL_JPY = 600_000.0


# ── ブートストラップ本体（純関数） ────────────────────────────────────────────


def bootstrap_annual_paths(
    pnl: np.ndarray,
    *,
    trades_per_year: int,
    n_sims: int,
    block_len: int,
    rng: np.random.Generator,
    cb_threshold: float = CB_THRESHOLD_JPY,
    checkpoints: tuple[int, ...] = (30, 60, 90),
) -> dict[str, np.ndarray]:
    """トレード損益列から年間パスを再抽出する（block_len=1 で iid）。

    循環ブロック・ブートストラップ: 開始位置を一様に選び、そこから
    block_len 件連続で取る（末尾は先頭に巻き戻す）。トレード間の
    短期クラスタ（連敗・レジームのかたまり）を保存する。

    戻り値: annual_pnl / cb_tripped（年内に純損失が閾値到達）/ max_dd
    """
    n = len(pnl)
    if n == 0 or trades_per_year <= 0:
        raise ValueError("pnl must be non-empty and trades_per_year positive")

    n_blocks = math.ceil(trades_per_year / block_len)
    # (n_sims, n_blocks) の開始位置 → (n_sims, n_blocks, block_len) の添字
    starts = rng.integers(0, n, size=(n_sims, n_blocks))
    offsets = np.arange(block_len)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    samples = pnl[idx].reshape(n_sims, -1)[:, :trades_per_year]

    cum = np.cumsum(samples, axis=1)
    annual_pnl = cum[:, -1]
    # CB: フェーズ開始からの純損失（-cum）がどこかで閾値以上になったか
    cb_tripped = (-cum).max(axis=1) >= cb_threshold
    # 最大ドローダウン（0 起点のピークからの落差）
    running_peak = np.maximum.accumulate(np.maximum(cum, 0), axis=1)
    max_dd = (cum - running_peak).min(axis=1)

    out = {"annual_pnl": annual_pnl, "cb_tripped": cb_tripped, "max_dd": max_dd}
    # 実測トラッキング用: 取引数 k 時点の累積損益（撤退判定の分布照合に使う）
    out["checkpoint_cum"] = {
        k: cum[:, k - 1] for k in checkpoints if k <= trades_per_year
    }
    return out


def summarize_paths(
    paths: dict[str, np.ndarray],
    *,
    initial_capital: float = INITIAL_CAPITAL_JPY,
    target_jpy: float = TARGET_ANNUAL_JPY,
) -> dict:
    """年間パス群から意思決定用の統計を出す。"""
    pnl = paths["annual_pnl"]
    pct = [5, 25, 50, 75, 95]
    return {
        "n_sims": int(len(pnl)),
        "p_cb_within_1y": float(paths["cb_tripped"].mean()),
        "p_annual_positive": float((pnl > 0).mean()),
        "p_annual_ge_target": float((pnl >= target_jpy).mean()),
        "annual_pnl_percentiles_jpy": {
            str(p): float(np.percentile(pnl, p)) for p in pct
        },
        "annual_return_percentiles_pct": {
            str(p): float(np.percentile(pnl, p) / initial_capital * 100) for p in pct
        },
        "annual_pnl_mean_jpy": float(pnl.mean()),
        "max_dd_percentiles_jpy": {
            str(p): float(np.percentile(paths["max_dd"], p)) for p in [5, 50]
        },
        # 実測照合表: 累計 k 取引時点の累積損益がこの下側パーセンタイル未満なら
        # 「モデルが正しくてもその確率でしか起きない悪さ」= モデル不適合のシグナル
        "checkpoint_cum_percentiles_jpy": {
            str(k): {str(p): float(np.percentile(v, p)) for p in [5, 25, 50]}
            for k, v in paths.get("checkpoint_cum", {}).items()
        },
    }


# ── Deflated Sharpe Ratio（多重検定割引） ────────────────────────────────────


def sharpe_per_trade(returns: np.ndarray) -> float:
    """トレード単位リターン（比率）の Sharpe（無リスク金利 0）。"""
    sd = returns.std(ddof=1)
    return float(returns.mean() / sd) if sd > 0 else 0.0


def expected_max_sharpe(var_trials: float, n_trials: int) -> float:
    """K 回の独立試行から最良を選んだとき期待されるノイズ Sharpe の最大値。

    Bailey & López de Prado (2014): E[max SR] ≈ sqrt(V[SR]) ×
    ((1-γ)Φ⁻¹(1-1/K) + γΦ⁻¹(1-1/(K·e)))
    """
    if n_trials <= 1 or var_trials <= 0:
        return 0.0
    sd = math.sqrt(var_trials)
    return sd * (
        (1 - _EULER_GAMMA) * _NORM.inv_cdf(1 - 1 / n_trials)
        + _EULER_GAMMA * _NORM.inv_cdf(1 - 1 / (n_trials * math.e))
    )


def deflated_sharpe_ratio(
    sr_obs: float, n_obs: int, skew: float, kurt: float, sr_benchmark: float,
) -> float:
    """観測 Sharpe が sr_benchmark（ノイズ期待最大値）を超えている確率。

    kurt は Pearson の尖度（正規分布 = 3）。
    """
    if n_obs < 2:
        return 0.0
    denom = math.sqrt(
        max(1e-12, 1 - skew * sr_obs + (kurt - 1) / 4 * sr_obs**2)
    )
    z = (sr_obs - sr_benchmark) * math.sqrt(n_obs - 1) / denom
    return float(_NORM.cdf(z))


def moments(returns: np.ndarray) -> tuple[float, float]:
    """(歪度, Pearson 尖度)。"""
    mu = returns.mean()
    sd = returns.std(ddof=0)
    if sd == 0:
        return 0.0, 3.0
    z = (returns - mu) / sd
    return float((z**3).mean()), float((z**4).mean())


def trial_sharpe_variance_from_grid(grid_results: list[dict]) -> tuple[float, int]:
    """グリッド探索結果から試行 Sharpe（トレード単位）の分散を推定する。

    grid の sharpe_ratio は年率化済み（per-trade SR × sqrt(trades_per_year)）
    のため、per-trade 単位に戻してから分散を取る。
    """
    srs = []
    for cell in grid_results:
        tpy = cell.get("trades_per_year") or 0
        sr_ann = cell.get("sharpe_ratio")
        if sr_ann is None or tpy <= 0:
            continue
        srs.append(sr_ann / math.sqrt(tpy))
    if len(srs) < 2:
        return 0.0, len(srs)
    arr = np.array(srs)
    return float(arr.var(ddof=1)), len(srs)


# ── 実行 ──────────────────────────────────────────────────────────────────────


def _collect_trades(start: date, end: date) -> list:
    """正準構成（本番デフォルトパラメータ）でバックテストしトレード列を返す。"""
    from backtest import load_cache, run_one_backtest

    data = load_cache(start, end)
    metrics = run_one_backtest(start, end, data, return_trades=True)
    return metrics["trade_records"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Monte Carlo 分布推定（V-4）")
    parser.add_argument("--sims", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--block", type=int, default=20, help="ブロック長 (default: 20)")
    parser.add_argument("--json", default=None, help="結果 JSON の出力先")
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)

    print("トレード列を生成中（正準構成・gap-aware）...", flush=True)
    trades_4y = _collect_trades(date(2022, 1, 4), date(2025, 12, 30))
    trades_24_25 = [t for t in trades_4y if t.exit_date >= date(2024, 1, 1)]
    trades_26h1 = _collect_trades(date(2026, 1, 5), date(2026, 6, 30))

    scenarios = {
        "full_4y": {
            "trades": trades_4y,
            "years": 4.0,
            "desc": "2022-2025 全レジーム混合（正準・保守側）",
        },
        "recent_regime": {
            "trades": trades_24_25 + trades_26h1,
            "years": 2.5,
            "desc": "2024-01〜2026-06（現行レジーム継続仮定・楽観側）",
        },
    }

    results: dict = {
        "config": "¥600k / pmax3000 / noTP/ATR3.0/TS5 / 2slots / gap-aware",
        "sims": args.sims,
        "seed": args.seed,
        "block_len": args.block,
        "cb_threshold_jpy": CB_THRESHOLD_JPY,
        "scenarios": {},
    }

    for name, sc in scenarios.items():
        pnl = np.array([float(t.pnl) for t in sc["trades"]])
        tpy = round(len(pnl) / sc["years"])
        sc_out = {
            "desc": sc["desc"],
            "n_trades": len(pnl),
            "trades_per_year": tpy,
            "empirical_annual_pnl_jpy": float(pnl.sum() / sc["years"]),
        }
        for mode, block in (("iid", 1), ("block", args.block)):
            paths = bootstrap_annual_paths(
                pnl, trades_per_year=tpy, n_sims=args.sims,
                block_len=block, rng=rng,
            )
            sc_out[mode] = summarize_paths(paths)
        results["scenarios"][name] = sc_out
        print(f"  {name}: {len(pnl)} trades, {tpy}/year", flush=True)

    # ── DSR（多重検定割引） ──────────────────────────────────────────────
    pnl_pct_4y = np.array([t.pnl_pct / 100 for t in trades_4y])
    sr_obs = sharpe_per_trade(pnl_pct_4y)
    skew, kurt = moments(pnl_pct_4y)
    n_obs = len(pnl_pct_4y)

    grid_path = Path(".backtest_cache/grid_search_results.json")
    var_trials, n_grid = (
        trial_sharpe_variance_from_grid(json.load(grid_path.open()))
        if grid_path.exists() else (0.0, 0)
    )

    # 試行数 K: 記録に残る探索ラウンド（180 + 96 + 松竹梅4ティア×96 ≈ 660）。
    # 正確な K は不可知のため感度で示す。
    dsr_out = {
        "sr_per_trade_obs": sr_obs,
        "sr_annualized_obs": sr_obs * math.sqrt(len(pnl_pct_4y) / 4.0),
        "n_trades": n_obs,
        "skew": skew,
        "kurtosis_pearson": kurt,
        "trial_sr_variance_source": f"grid_search_results.json ({n_grid} combos)",
        "trial_sr_variance": var_trials,
        "by_n_trials": {},
    }
    for k in (100, 300, 660, 1000):
        sr0 = expected_max_sharpe(var_trials, k)
        dsr_out["by_n_trials"][str(k)] = {
            "expected_max_noise_sr": sr0,
            "dsr": deflated_sharpe_ratio(sr_obs, n_obs, skew, kurt, sr0),
        }
    results["dsr"] = dsr_out

    # ── 出力 ────────────────────────────────────────────────────────────
    print()
    for name, sc in results["scenarios"].items():
        b = sc["block"]
        print(f"=== {name}: {sc['desc']} ===")
        print(f"  実測年間損益: ¥{sc['empirical_annual_pnl_jpy']:+,.0f} "
              f"({sc['n_trades']} trades)")
        print(f"  P(CB 1年内発動)   : {b['p_cb_within_1y'] * 100:.1f}%  (iid: "
              f"{sc['iid']['p_cb_within_1y'] * 100:.1f}%)")
        print(f"  P(年間黒字)       : {b['p_annual_positive'] * 100:.1f}%")
        print(f"  P(年 ¥100k 達成)  : {b['p_annual_ge_target'] * 100:.1f}%")
        pp = b["annual_pnl_percentiles_jpy"]
        print(f"  年間損益 90% 区間 : ¥{pp['5']:+,.0f} 〜 ¥{pp['95']:+,.0f} "
              f"(中央値 ¥{pp['50']:+,.0f})")
    print("=== Deflated Sharpe Ratio（多重検定割引） ===")
    print(f"  観測 Sharpe（年率）: {dsr_out['sr_annualized_obs']:.2f}  "
          f"(per-trade {sr_obs:.3f}, n={n_obs})")
    for k, v in dsr_out["by_n_trials"].items():
        print(f"  K={k:>4} 試行: ノイズ期待最大 SR={v['expected_max_noise_sr']:.3f} "
              f"→ DSR={v['dsr'] * 100:.1f}%")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
        print(f"\nJSON: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
