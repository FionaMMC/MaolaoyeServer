#!/usr/bin/env bash
# 把 V20H 历史数据从 Mac 上传到 server。
# 用法: ./scripts/upload_v20h_data.sh <BASE_URL> <API_KEY> <LOCAL_V20H_DATA_DIR>
set -euo pipefail

BASE="${1:-http://120.26.138.82:8000}"
KEY="${2:-pipeline-v23-shared-secret-2026}"
LOCAL_DIR="${3:-/Users/mameican/Desktop/server/v2.3/server/plugins/v20h/data}"

for fn in pred_csi1000.parquet v12_exp_hs300.parquet stock_close.parquet \
          stock_returns.parquet index_csi1000.parquet; do
  if [ -f "$LOCAL_DIR/$fn" ]; then
    size=$(du -h "$LOCAL_DIR/$fn" | cut -f1)
    echo "uploading $fn ($size)..."
    curl -X POST "$BASE/admin/upload-data?strategy=v20h_v1_3&filename=$fn" \
      -H "Authorization: Bearer $KEY" \
      -F "file=@$LOCAL_DIR/$fn" \
      --progress-bar
    echo ""
  else
    echo "WARNING: $fn not found, skipping"
  fi
done

echo "status:"
curl -s -H "Authorization: Bearer $KEY" \
  "$BASE/admin/data-status?strategy=v20h_v1_3" | python3 -m json.tool
