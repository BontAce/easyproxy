#!/bin/bash
export PYTHONPATH=/app

# --- Cloudflare WARP Setup ---
if [ "$ENABLE_WARP" = "true" ]; then
    echo "🌐 Starting Cloudflare WARP..."
    # Ensure /dev/net/tun exists
    if [ ! -c /dev/net/tun ]; then
        echo "⚠️ /dev/net/tun not found. WARP might not work. Ensure --cap-add=NET_ADMIN and --device /dev/net/tun are used."
    fi

    # Start warp-svc and suppress noisy hardware/dbus warnings
    warp-svc --accept-tos > /var/log/warp-svc.log 2>&1 &
    
    # Wait for warp-svc to be ready
    MAX_RETRIES=15
    COUNT=0
    while ! warp-cli --accept-tos status > /dev/null 2>&1; do
        echo "⏳ Waiting for warp-svc... ($COUNT/$MAX_RETRIES)"
        sleep 1
        COUNT=$((COUNT+1))
        if [ $COUNT -ge $MAX_RETRIES ]; then
            echo "❌ Failed to start warp-svc"
            break
        fi
    done

    if [ $COUNT -lt $MAX_RETRIES ]; then
        # Register if needed
        if ! warp-cli --accept-tos status | grep -q "Registration Name"; then
             echo "📝 Registering WARP..."
             # Delete old registration if it exists to avoid "Old registration is still around" error
             warp-cli --accept-tos registration delete > /dev/null 2>&1 || true
             warp-cli --accept-tos registration new
        fi
        
        # Set license key if provided
        if [ -n "$WARP_LICENSE_KEY" ]; then
            echo "🔑 Setting WARP license key..."
            warp-cli --accept-tos set-license "$WARP_LICENSE_KEY"
        fi
        
        # Connect
        echo "🔗 Connecting to WARP..."
        
        # Add exclusions for domains that block WARP (Cinemacity)
        # We try both new (v2024+) and old warp-cli commands for compatibility
        (warp-cli --accept-tos tunnel host add cinemacity.cc > /dev/null 2>&1 || \
         warp-cli --accept-tos add-excluded-domain cinemacity.cc > /dev/null 2>&1) || true
         
        (warp-cli --accept-tos tunnel host add cccdn.net > /dev/null 2>&1 || \
         warp-cli --accept-tos add-excluded-domain cccdn.net > /dev/null 2>&1) || true
        
        # Set mode to proxy (SOCKS5) and force port 1080
        warp-cli --accept-tos set-mode proxy
        warp-cli --accept-tos set-proxy-port 1080
        
        warp-cli --accept-tos connect
        
        # Small delay for connection to stabilize
        sleep 3
        warp-cli --accept-tos status
    fi
fi

# Start FlareSolverr in the background
echo "🚀 Starting FlareSolverr (v3 Python)..."
if [ "$ENABLE_WARP" = "true" ]; then
    export PROXY="socks5://127.0.0.1:1080"
fi
cd /app/flaresolverr && PORT=8191 python3 src/flaresolverr.py &

# Start Byparr in the background
echo "🛡️ Starting Byparr..."
if [ "$ENABLE_WARP" = "true" ]; then
    export GLOBAL_PROXY="socks5://127.0.0.1:1080"
fi
cd /app/byparr_src && PORT=8192 python3 main.py &

# Start EasyProxy (Gunicorn)
echo "🎬 Starting EasyProxy..."
cd /app
WORKERS_COUNT=${WORKERS:-$(nproc 2>/dev/null || echo 1)}
xvfb-run -a --server-args='-screen 0 1366x768x24' gunicorn --bind 0.0.0.0:${PORT:-7860} --workers $WORKERS_COUNT --worker-class aiohttp.worker.GunicornWebWorker --timeout 120 --graceful-timeout 120 app:app
