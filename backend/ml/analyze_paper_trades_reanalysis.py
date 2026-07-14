"""
[정식 자동화] 31.9% 격차 재분석 — model_version 기반 정확한 버전

이전 verify_paper_trades_vs_model.py는 "그 시점 가장 최근 모델"을 추정해야 해서
실제 서빙 모델과 다를 수 있었다(rank 일치율 3.6%). paper_trades.model_version
컬럼이 생긴 이후로는 각 거래가 정확히 어느 모델로 추천됐는지 알 수 있으므로,
그 모델을 그대로 불러와 진짜 순위/확률을 재현한다.

check_reanalysis_ready.py가 조건(기본 30건) 충족 시 이 스크립트를 호출한다.
직접 실행도 가능: python analyze_paper_trades_reanalysis.py [--threshold 30]

출력: backend/reanalysis_report.md
"""

import argparse
import json
import pickle
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# Windows cp949 콘솔에서 UTF-8 문자 출력 시 UnicodeEncodeError 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import pandas as pd

from dataset import DB_PATH
from predictor import predict_today

MODELS_DIR = Path(__file__).parent.parent / "models"
REPORT_PATH = Path(__file__).parent.parent / "reanalysis_report.md"
_UNIFIED_NAME_RE = re.compile(r"^target_5d_\d{8}_\d{6}$")


def load_cat_model(version: str):
    path = MODELS_DIR / version / "model_cat.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def latest_backtest_winrate() -> Optional[Tuple[float, str]]:
    """가장 최근 통합 모델의 self-val 백테스트 승률 (비교 기준선)."""
    cands = [
        p for p in MODELS_DIR.glob("target_5d_*/backtest_5d_data.json")
        if _UNIFIED_NAME_RE.match(p.parent.name)
    ]
    if not cands:
        return None
    latest = max(cands, key=lambda p: p.parent.stat().st_mtime)
    data = json.loads(latest.read_text(encoding="utf-8"))
    win_rate = data.get("win_rate")
    return (win_rate, latest.parent.name) if win_rate is not None else None


def run(threshold: int = 30) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM paper_trades WHERE status='closed' AND model_version IS NOT NULL "
            "ORDER BY recommended_date",
            conn,
        )

    if len(df) < threshold:
        print(f"표본 부족({len(df)}/{threshold}) — 리포트 생성 안 함")
        return 1

    rows = []
    model_cache = {}
    for version, grp in df.groupby("model_version"):
        if version not in model_cache:
            model_cache[version] = load_cat_model(version)
        model = model_cache[version]
        if model is None:
            for _, t in grp.iterrows():
                rows.append({**t.to_dict(), "true_rank": None, "true_proba": None})
            continue
        for date, date_grp in grp.groupby("recommended_date"):
            raw = predict_today(model, date, top_n=100)
            raw_sorted = sorted(raw, key=lambda p: -p["probability"]) if raw else []
            rank_map = {p["symbol"]: (i + 1, p["probability"]) for i, p in enumerate(raw_sorted)}
            for _, t in date_grp.iterrows():
                tr, tp = rank_map.get(t["symbol"], (None, None))
                rows.append({**t.to_dict(), "true_rank": tr, "true_proba": tp})

    result = pd.DataFrame(rows)
    result["win"] = result["return_pct"] > 0

    overall_win = float(result["win"].mean())
    overall_avg = float(result["return_pct"].mean())
    rank_known = result["true_rank"].notna()
    rank_match = float((result.loc[rank_known, "true_rank"] == result.loc[rank_known, "recommended_rank"]).mean()) if rank_known.any() else None

    result["rank_bucket"] = pd.cut(
        result["true_rank"], bins=[0, 3, 10, 20, 9999],
        labels=["1-3위", "4-10위", "11-20위", "21위+"],
    )
    bucket_stats = result.groupby("rank_bucket", observed=True).agg(
        n=("win", "size"), win_rate=("win", "mean"), avg_ret=("return_pct", "mean")
    )

    bt = latest_backtest_winrate()

    lines = []
    lines.append("# 모의투자 31.9% 격차 재분석 리포트\n\n")
    lines.append(f"생성 시각: {datetime.now().isoformat()}\n\n")
    lines.append(
        f"분석 대상: `model_version` 기록된 청산완료 거래 {len(result)}건 "
        f"(모델 {result['model_version'].nunique()}개 버전)\n\n"
    )
    lines.append("## 종합\n\n")
    lines.append(f"- 전체 실제 승률: **{overall_win:.3f}**\n")
    lines.append(f"- 전체 평균 5일 수익률: **{overall_avg:.2f}%**\n")
    if rank_match is not None:
        lines.append(f"- DB 기록 순위 vs 실제 서빙모델 재현 순위 일치율: **{rank_match*100:.1f}%** "
                      f"(낮으면 model_version 기록 자체에 문제가 있다는 뜻)\n")
    if bt:
        gap = (bt[0] - overall_win) * 100
        lines.append(f"- 참고: 최신 모델 self-val 백테스트 승률 {bt[0]:.3f} ({bt[1]})\n")
        lines.append(f"- **백테스트 대비 격차: {gap:.1f}%p**\n")
    lines.append("\n## rank 구간별 실제 성과\n\n")
    lines.append("| 구간 | n | 승률 | 평균수익률 |\n|---|---|---|---|\n")
    for idx, row in bucket_stats.iterrows():
        lines.append(f"| {idx} | {int(row['n'])} | {row['win_rate']:.3f} | {row['avg_ret']:.2f}% |\n")

    lines.append("\n## 모델 버전별 성과\n\n")
    lines.append("| model_version | n | 승률 | 평균수익률 |\n|---|---|---|---|\n")
    for version, grp in result.groupby("model_version"):
        lines.append(f"| {version} | {len(grp)} | {grp['win'].mean():.3f} | {grp['return_pct'].mean():.2f}% |\n")

    REPORT_PATH.write_text("".join(lines), encoding="utf-8")
    print(f"리포트 저장: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="31.9% 격차 재분석 (model_version 기반)")
    parser.add_argument("--threshold", type=int, default=30)
    args = parser.parse_args()
    sys.exit(run(args.threshold))
