#!/bin/bash
echo "ネットワークラボ エミュレーター 起動中..."
cd "$(dirname "$0")"
pip install -r requirements.txt -q 2>/dev/null
python app.py
