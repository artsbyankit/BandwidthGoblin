#!/bin/sh
cd "$(dirname "$0")"
python3 linkboost.py
echo "--- done, press Enter to close ---"
read x
