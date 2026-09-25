#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "イカリング3アーカイブ 統合リモート同期"
echo "=========================================="

echo ""
echo "[1/2] NAS同期を実行中..."
"$SCRIPT_DIR/sync_nas.sh" "$@"

echo ""
echo "[2/2] Google Drive同期を実行中..."
"$SCRIPT_DIR/sync_gdrive.sh" "$@"

echo ""
echo "=========================================="
echo "全リモート同期が完了しました"
echo "=========================================="
