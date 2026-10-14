#!/data/data/com.termux/files/usr/bin/bash
set -e

echo ""
echo "=================================================================="
echo "👑 ANTIGRAVITY AI BUTLER - ANDROID 24/7 MINI-SERVER O'RNATILMOQDA"
echo "=================================================================="
echo ""

# 1. Acquire Wake-Lock so CPU stays awake when screen is off
if command -v termux-wake-lock &>/dev/null; then
    termux-wake-lock
    echo "🔋 [1/4] Termux Wake-Lock faollashtirildi (Ekran o'chsa ham to'xtamaydi)."
fi

# 2. Pre-install pydantic-core and pydantic (Instant, Zero Rust compilation)
echo "📚 [2/6] Pydantic va Pydantic-Core o'rnatilmoqda..."
if ! python -c "import pydantic" 2>/dev/null; then
    if [ -f "wheels/pydantic_core-2.41.5-cp313-cp313-android_24_arm64_v8a.whl" ]; then
        pip install --break-system-packages wheels/pydantic_core-2.41.5-cp313-cp313-android_24_arm64_v8a.whl 2>/dev/null || {
            echo "Wheel to'g'ridan-to'g'ri site-packages ga ochilmoqda..."
            SITE_DIR=$(python -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || echo "")
            if [ -n "$SITE_DIR" ]; then
                python -m zipfile -e wheels/pydantic_core-2.41.5-cp313-cp313-android_24_arm64_v8a.whl "$SITE_DIR" 2>/dev/null || true
            fi
        }
    fi
    pip install --break-system-packages --extra-index-url https://termux-user-repository.github.io/pypi/ "pydantic-core>=2.20.0" 2>/dev/null || true
    pip install --break-system-packages --no-deps "pydantic>=2.7.0" "pydantic-settings>=2.0.0" 2>/dev/null || true
    if ! python -c "import pydantic" 2>/dev/null; then
        pkg install -y python-pydantic 2>/dev/null || true
    fi
fi

# 3. Ensure cryptography is pre-installed at system level (Prevents Rust compilation)
echo "🔐 [3/6] Cryptography tizim darajasida tekshirilmoqda..."
if ! python -c "import cryptography" 2>/dev/null; then
    echo "Kutubxona yuklanmoqda (pkg install python-cryptography)..."
    pkg install -y python-cryptography 2>/dev/null || apt install -y python-cryptography 2>/dev/null || true
fi

# 4. Install remaining requirements
echo "📚 [4/6] Qolgan barcha kutubxonalar o'rnatilmoqda (Telethon, Aiogram, Gemini)..."
pip install --break-system-packages -r requirements.txt

# 5. Prepare data directories
mkdir -p data data/backups

# 6. Create standalone Watchdog runner
echo "🛡️ [5/6] 24/7 Avtomatik tirilish (Watchdog) sozlanmoqda..."
cat << 'RUNNER_EOF' > run_butler.sh
#!/data/data/com.termux/files/usr/bin/bash
if command -v termux-wake-lock &>/dev/null; then
    termux-wake-lock
fi

cd "$HOME/butler" 2>/dev/null || true

# Rotate logs if larger than 5MB
LOG_FILE="butler_phone.log"
if [ -f "$LOG_FILE" ] && [ $(wc -c < "$LOG_FILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOG_FILE" "${LOG_FILE}.old" 2>/dev/null || true
fi

while true; do
    echo "[$(date)] 🛡️ Antigravity Tier-4 Watchdog Supervisor ishga tushirilmoqda..."
    python scripts/watchdog_supervisor.py
    EXIT_CODE=$?
    echo "[$(date)] Supervisor to'xtadi (kod: $EXIT_CODE). 5 soniyadan keyin avto-restart..."
    sleep 5
done
RUNNER_EOF
chmod +x run_butler.sh

# 5. Stop any previous instance and start fresh
pkill -f "watchdog_supervisor.py" 2>/dev/null || true
pkill -f "python main.py" 2>/dev/null || true
pkill -f "run_butler.sh" 2>/dev/null || true

echo "🚀 [6/6] Butler 24/7 fon rejimida ishga tushirilmoqda..."
nohup ./run_butler.sh > butler_phone.log 2>&1 &

# 7. Configure auto-revive on phone boot & Termux launch
mkdir -p "$HOME/.termux/boot"
cat << 'BOOT_EOF' > "$HOME/.termux/boot/start_butler.sh"
#!/data/data/com.termux/files/usr/bin/bash
if command -v termux-wake-lock &>/dev/null; then
    termux-wake-lock
fi
cd "$HOME/butler" && nohup ./run_butler.sh > butler_phone.log 2>&1 &
BOOT_EOF
chmod +x "$HOME/.termux/boot/start_butler.sh"

if ! grep -q "run_butler.sh" "$HOME/.bashrc" 2>/dev/null; then
    cat << 'BASHRC_EOF' >> "$HOME/.bashrc"

# Antigravity Butler Auto-Guardian
if ! pgrep -f "python main.py" > /dev/null; then
    (cd "$HOME/butler" && nohup ./run_butler.sh > butler_phone.log 2>&1 &)
fi
BASHRC_EOF
fi

sleep 3

echo ""
echo "=================================================================="
echo "🎉 TABRIKLAYMIZ! BUTLER TELEFONINGIZDA 24/7 ISHLAYAPTI!"
echo "------------------------------------------------------------------"
echo "✅ Quvvat: Telefon internetga ulanib tursa, butun umr ishlaydi."
echo "✅ Ekran o'chiq tursa ham to'xtamaydi (Zero-Sleep)."
echo "✅ Loglarni ko'rish: tail -f butler_phone.log"
echo "=================================================================="
echo ""
