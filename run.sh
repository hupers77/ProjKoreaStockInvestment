#!/usr/bin/env bash
# macOS / Linux 실행 스크립트
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[설치] 처음 실행: 가상환경을 만들고 필요한 패키지를 설치합니다..."
  python3 -m venv .venv || { echo "Python 3.10 이상을 먼저 설치하세요."; exit 1; }
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
