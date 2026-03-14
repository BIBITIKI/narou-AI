"""
cron用: 登録済み全作品の新話チェック・更新
Railway の cron サービスから呼び出す。

環境変数:
    NAROU_NCODES   カンマ区切りのNコードリスト  例: n9999zz,n1234ab
    GEMINI_API_KEY
"""

import os, sys, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pipeline import run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger()

ncodes_raw = os.environ.get("NAROU_NCODES", "")
ncodes = [n.strip() for n in ncodes_raw.split(",") if n.strip()]

if not ncodes:
    log.info("NAROU_NCODES が未設定。終了。")
    sys.exit(0)

log.info(f"対象作品: {ncodes}")
errors = []
for ncode in ncodes:
    try:
        log.info(f"--- {ncode} 開始 ---")
        run(ncode, update_only=True)
    except Exception as e:
        log.error(f"{ncode} 失敗: {e}")
        errors.append(ncode)

if errors:
    log.error(f"失敗: {errors}")
    sys.exit(1)

log.info("全作品更新完了")
