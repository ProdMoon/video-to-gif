#!/bin/bash
set -e
PYTHON=/Users/teamsparta/.local/bin/python3
$PYTHON -m pip install -r requirements.txt
mkdir -p output
echo "Setup complete. Run: $PYTHON app.py"
