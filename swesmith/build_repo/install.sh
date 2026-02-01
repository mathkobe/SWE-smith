#!/bin/bash
set -e

# --------------------------
# 环境变量
# --------------------------
ENV_NAME=${ENV_NAME:-testbe}
PYTHON_VERSION=${SWESMITH_PYTHON_VERSION:-3.10}

# owner/repo 信息，用于生成 create_images 期望的目录
OWNER=${SWESMITH_OWNER:-Instagram}
REPO=${SWESMITH_REPO:-MonkeyType}
COMMIT=${SWESMITH_COMMIT:-70c3acf6}

# 构建目标目录和 yml 文件名
OUTPUT_DIR="logs/build_images/env/${OWNER}__${REPO}.${COMMIT}"
OUTPUT_YML="${OUTPUT_DIR}/sweenv_${OWNER}__${REPO}.${COMMIT}.yml"

echo "Creating conda env: $ENV_NAME"

# --------------------------
# 创建 conda 环境
# --------------------------
conda create -y -n $ENV_NAME python=$PYTHON_VERSION -c conda-forge -c defaults
source activate $ENV_NAME

# --------------------------
# 基础 pip 工具升级
# --------------------------
pip install --upgrade pip setuptools wheel

# --------------------------
# 安装 repo
# --------------------------
pip install -e ".[dev]"

# 运行 profile install cmds（如果有）
if [ -n "$SWESMITH_PROFILE_INSTALL_CMDS" ]; then
    echo "Running custom profile install commands..."
    eval "$SWESMITH_PROFILE_INSTALL_CMDS"
fi

# 安装额外 test deps（如果有）
if [ -n "$SWESMITH_EXTRA_TEST_DEPS" ]; then
    echo "Installing extra test dependencies: $SWESMITH_EXTRA_TEST_DEPS"
    pip install $SWESMITH_EXTRA_TEST_DEPS
fi

# --------------------------
# 导出 conda 环境到 create_images 期望的路径
# --------------------------
mkdir -p "$OUTPUT_DIR"
conda env export -n $ENV_NAME > "$OUTPUT_YML"

# --------------------------
# 安装完成提示
# --------------------------
echo "Installation complete!"
echo "> Exported conda environment to $OUTPUT_YML"
