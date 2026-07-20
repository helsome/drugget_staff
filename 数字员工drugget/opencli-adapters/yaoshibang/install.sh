#!/bin/bash
# 药师帮 opencli 适配器安装脚本
# 将适配器安装到 ~/.opencli/clis/ 目录下

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADAPTER_DIR="$SCRIPT_DIR/clis/yaoshibang"
TARGET_DIR="$HOME/.opencli/clis/yaoshibang"

echo "==> 安装药师帮 opencli 适配器"

# 检查 opencli 是否已安装
if ! command -v opencli &> /dev/null; then
    echo "ERROR: opencli 未安装，请先运行: npm install -g @jackwener/opencli"
    exit 1
fi

echo "    opencli 版本: $(opencli --version)"

# 创建目标目录
mkdir -p "$TARGET_DIR"

# 复制适配器文件
echo "    复制适配器文件..."
cp "$ADAPTER_DIR/auth.js" "$TARGET_DIR/"
cp "$ADAPTER_DIR/search.js" "$TARGET_DIR/"
cp "$ADAPTER_DIR/detail.js" "$TARGET_DIR/"
cp "$ADAPTER_DIR/shop.js" "$TARGET_DIR/"
cp "$ADAPTER_DIR/resolve-provider.js" "$TARGET_DIR/"

# 验证安装
echo "    验证安装..."
if opencli list 2>/dev/null | grep -q yaoshibang; then
    echo "==> 安装成功！"
    echo ""
    echo "可用命令:"
    echo "  opencli yaoshibang whoami          - 查看登录状态"
    echo "  opencli yaoshibang search <关键词>  - 搜索药品"
    echo "  opencli yaoshibang detail <商品ID> --provider_id <供应商ID>  - 查看商品详情"
    echo "  opencli yaoshibang shop <供应商ID>  - 查看店铺信息"
    echo "  opencli yaoshibang resolve-provider <店铺名>  - 解析供应商ID"
else
    echo "==> 文件已复制，但 opencli 未识别到 yaoshibang。"
    echo "    请确认 opencli 是否正确读取 ~/.opencli/clis/ 目录。"
fi
