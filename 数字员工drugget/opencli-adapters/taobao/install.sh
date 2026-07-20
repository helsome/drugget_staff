#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="$HOME/.opencli/clis/taobao"
mkdir -p "$TARGET_DIR"
cp "$SCRIPT_DIR/clis/taobao/shop-search.js" "$TARGET_DIR/"
echo "淘宝店内搜索适配器已安装：opencli taobao shop-search"
