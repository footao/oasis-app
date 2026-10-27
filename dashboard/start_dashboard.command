#!/bin/bash
# ダブルクリックで起動（Mac）。初回だけ「ターミナル」で  chmod +x start_dashboard.command  を実行してください。
cd "$(dirname "$0")"
exec python3 oasis_dashboard.py
