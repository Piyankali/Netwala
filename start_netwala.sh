#!/data/data/com.termux/files/usr/bin/bash

KEY_FILE="$HOME/.netwala/key"
LOG_FILE="$HOME/netwala_server.log"

# Load/generate key
if [ ! -f "$KEY_FILE" ]; then
    echo "[!] Generating NETWALA_KEY..."
    mkdir -p "$HOME/.netwala"
    python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
fi
NETWALA_KEY=$(cat "$KEY_FILE")
export NETWALA_VERBOSE="${NETWALA_VERBOSE:-0}"
# input-safety policy. Loopback/internal is ON by default (authorized internal
# and SSRF testing needs it); flip the individual knobs to tighten up.
export NETWALA_ALLOW_LOOPBACK="${NETWALA_ALLOW_LOOPBACK:-1}"
export NETWALA_ALLOW_FILE="${NETWALA_ALLOW_FILE:-0}"
export NETWALA_IO_DIRS="${NETWALA_IO_DIRS:-}"

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
fi

# Kill previous (graceful then force)
pkill -f "netwala.py" 2>/dev/null
pkill -f "proot-distro login debian.*netwala" 2>/dev/null
sleep 2

# Wait for port 8765 to be free (max 10s)
echo "[+] Waiting for port 8765 to be free..."
for i in $(seq 1 10); do
    if ! ss -tlnp 2>/dev/null | grep -q ":8765 " && ! netstat -tln 2>/dev/null | grep -q ":8765 "; then
        break
    fi
    sleep 1
done

# Extra safety: if still bound, force kill anything on it
if ss -tlnp 2>/dev/null | grep -q ":8765 " || netstat -tln 2>/dev/null | grep -q ":8765 "; then
    echo "[!] Port 8765 still bound, force-killing..."
    fuser -k 8765/tcp 2>/dev/null || true
    sleep 2
fi

# Launch with env passthrough
nohup proot-distro login debian -- env \
    NETWALA_KEY="$NETWALA_KEY" \
    NETWALA_VERBOSE="$NETWALA_VERBOSE" \
    NETWALA_HEADED="${NETWALA_HEADED:-1}" \
    NETWALA_DISPLAY="${NETWALA_DISPLAY:-:99}" \
    NETWALA_ALLOW_LOOPBACK="$NETWALA_ALLOW_LOOPBACK" \
    NETWALA_ALLOW_FILE="$NETWALA_ALLOW_FILE" \
    NETWALA_IO_DIRS="$NETWALA_IO_DIRS" \
    bash -lc '
    export HOME=/root
    export PYTHONPATH=/root/netwala
    # Headed mode needs an X server: headless Firefox has NO WebGL at all,
    # which is one of the strongest bot signals there is.
    if [ "$NETWALA_HEADED" = "1" ]; then
        if ! pgrep -f "Xvfb $NETWALA_DISPLAY" >/dev/null 2>&1; then
            if command -v Xvfb >/dev/null 2>&1; then
                nohup Xvfb "$NETWALA_DISPLAY" -screen 0 1366x768x24 -nolisten tcp >/dev/null 2>&1 &
                sleep 2
            fi
        fi
    fi
    export DISPLAY="$NETWALA_DISPLAY"
    export LIBGL_ALWAYS_SOFTWARE=1
    cd /root/netwala
    exec python3 netwala.py --transport streamable-http \
        --host 127.0.0.1 --port 8765
' > "$LOG_FILE" 2>&1 &

echo "[+] Launched PID $! (log: $LOG_FILE)"

# Wait & health check (retry 3x with delays — server boots in ~5-10s)
for attempt in 1 2 3; do
    sleep 5
    if curl -s -m 3 http://127.0.0.1:8765/health | grep -q '"status":"ok"'; then
        echo "✅ netwala started (attempt $attempt, NETWALA_KEY from $KEY_FILE)"
        exit 0
    fi
    echo "[~] Attempt $attempt: not yet, waiting more..."
done

echo "❌ Server failed after 3 attempts. Last 20 log lines:"
tail -20 "$LOG_FILE"
exit 1
