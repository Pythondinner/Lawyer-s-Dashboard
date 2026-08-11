#!/usr/bin/env python3
import os
import sys
import subprocess

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

def main():
    print("=" * 60)
    print("🚀 法律合规 Agent 启动中...")
    print("=" * 60)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("⚠️ 警告: 未设置 DEEPSEEK_API_KEY 环境变量")
        print("   请运行: export DEEPSEEK_API_KEY='your-api-key'")
        print("=" * 60)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    subprocess.run([
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.port", "8501",
        "--server.address", "0.0.0.0"
    ], env=env)

if __name__ == "__main__":
    main()
