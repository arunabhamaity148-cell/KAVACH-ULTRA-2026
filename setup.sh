#!/bin/bash
# KAVACH-ULTRA 2026 — setup.sh
# One-command VPS setup + deploy all fixes
# Usage: bash setup.sh

set -e
echo "🛡 KAVACH-ULTRA 2026 — Setup & Fix Deploy"
echo "==========================================="

cd ~/KAVACH-ULTRA-2026

# BUG #6: Fix python command
echo "📦 Step 1: Fixing python command..."
if ! command -v python &> /dev/null; then
    sudo apt-get install -y python-is-python3 2>/dev/null || \
    sudo ln -sf /usr/bin/python3 /usr/bin/python
    echo "✅ python → python3 linked"
else
    echo "✅ python already available"
fi

# Activate venv
echo "📦 Step 2: Activating virtualenv..."
source venv/bin/activate

# Fix all 'n' prefix bugs from GitHub paste
echo "🔧 Step 3: Fixing indentation bugs in all files..."
for f in signal_bot.py core/ai_brain.py core/data_engine.py utils/lead_lag.py utils/telegram_bot.py; do
    if [ -f "$f" ]; then
        sed -i 's/^n//' "$f"
        echo "  Fixed: $f"
    fi
done

# Syntax check all files
echo "🔍 Step 4: Syntax checking all files..."
ALL_OK=true
for f in signal_bot.py core/ai_brain.py core/data_engine.py utils/lead_lag.py utils/telegram_bot.py; do
    if [ -f "$f" ]; then
        if python -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
            echo "  ✅ $f"
        else
            echo "  ❌ $f — SYNTAX ERROR"
            ALL_OK=false
        fi
    fi
done

if [ "$ALL_OK" = false ]; then
    echo ""
    echo "❌ Some files still have syntax errors. Check above."
    exit 1
fi

# Install/update dependencies
echo "📦 Step 5: Installing dependencies..."
pip install -r requirements.txt -q

# Install systemd service
echo "⚙️  Step 6: Installing systemd service..."
if [ -f "kavach.service" ]; then
    sudo cp kavach.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable kavach
    echo "✅ Service installed"
else
    echo "⚠️  kavach.service not found — skipping"
fi

# Start/restart bot
echo "🚀 Step 7: Starting bot..."
sudo systemctl restart kavach 2>/dev/null || python signal_bot.py &

echo ""
echo "✅ DONE! Bot is running."
echo ""
echo "📋 Useful commands:"
echo "  sudo journalctl -u kavach -f     ← live logs"
echo "  sudo systemctl status kavach     ← status"
echo "  sudo systemctl restart kavach    ← restart"
echo "  sudo systemctl stop kavach       ← stop"
