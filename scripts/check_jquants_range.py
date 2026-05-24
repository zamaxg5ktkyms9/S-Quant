"""J-Quants Light Plan の取得可能期間を実測（A1 事前検証用・後で削除）。

各年の 1月上旬に 1 銘柄だけ叩いて、空応答 (None) か実データかを確認する。
"""

import os
import sys
from datetime import date

sys.path.insert(0, "src")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from squant.infrastructure.jquants_client import JQuantsClient, _RateLimiter

api_key = os.environ.get("JQUANTS_API_KEY", "")
if not api_key:
    print("ERROR: JQUANTS_API_KEY not set")
    sys.exit(1)

client = JQuantsClient(api_key=api_key, requests_per_minute=30)

print(f"Connectivity (2024-01-04): {client.check_connectivity()}")
print()

limiter = _RateLimiter(rpm=30, deadline=None)
for year in [2019, 2020, 2021, 2022, 2023]:
    df = client._fetch_daily_quotes(
        "7203.T", date(year, 1, 6), date(year, 1, 31), limiter
    )
    if df is None or df.empty:
        print(f"{year}-01: NO DATA")
    else:
        print(f"{year}-01: rows={len(df)}  first={df.index[0].date()}  last={df.index[-1].date()}")
