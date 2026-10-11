#!/bin/bash
# ==============================================================================
# Antigravity Telegram AI Executive Butler - 24/7 Production Deployment Script
# Supports: Ubuntu 20.04+, Debian 11+, CentOS 8+
# ==============================================================================

set -e

echo "🚀 [1/5] Tizim yangilanmoqda va zarur kutubxonalar o'rnatilmoqda..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl ufw

APP_DIR="/opt/telegram_ai_butler"
CURRENT_USER=$(whoami)

echo "📁 [2/5] Ilova papkasi tayyorlanmoqda: $APP_DIR"
sudo mkdir -p $APP_DIR
sudo chown -R $CURRENT_USER:$CURRENT_USER $APP_DIR

# Agar hozirgi jilddan nusxalanayotgan bo'lsa
if [ -f "main.py" ]; then
    echo "📦 Fayllar nusxalanmoqda..."
    cp -r * $APP_DIR/
    cp -r .env $APP_DIR/ 2>/dev/null || true
fi

cd $APP_DIR

echo "🐍 [3/5] Python virtual muhiti (venv) yaratilmoqda..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "⚙️ [4/5] Systemd 24/7 to'xtovsiz xizmati sozlanmoqda..."
sudo touch /var/log/butler.log /var/log/butler_error.log
sudo chown $CURRENT_USER:$CURRENT_USER /var/log/butler.log /var/log/butler_error.log
SERVICE_FILE="/etc/systemd/system/butler.service"

sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=Antigravity Telegram AI Executive Butler Daemon
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python main.py
Restart=always
RestartSec=5s
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/var/log/butler.log
StandardError=append:/var/log/butler_error.log

[Install]
WantedBy=multi-user.target
EOF

echo "🔄 [5/5] Xizmat ishga tushirilmoqda va avto-yuklanishga qo'shilmoqda..."
sudo systemctl daemon-reload
sudo systemctl enable butler
sudo systemctl restart butler

echo ""
echo "=============================================================================="
echo "✨ TABRIKLAYMIZ! Telegram AI Butler 24/7 to'xtovsiz rejimda ishga tushdi!"
echo "=============================================================================="
echo "📊 Holatni tekshirish buyrug'i: sudo systemctl status butler"
echo "📜 Jonli loglarni ko'rish:      sudo journalctl -u butler -f"
echo "🛑 To'xtatish buyrug'i:         sudo systemctl stop butler"
echo "🔄 Qayta ishga tushirish:       sudo systemctl restart butler"
echo "=============================================================================="
