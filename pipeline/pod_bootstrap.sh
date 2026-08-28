#!/usr/bin/env bash
# Bootstrap a RunPod GPU pod for Falsafa audiobook narration.
#
# Expects in env: R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_S3_ENDPOINT
#                 SHARD_BASE (this pod's first shard index) SHARDS_TOTAL PROCS
set -euo pipefail

echo "[boot] $(date -u) starting on $(hostname)"
export DEBIAN_FRONTEND=noninteractive

# --- system deps -----------------------------------------------------------
if ! command -v ffmpeg >/dev/null; then
  apt-get update -qq && apt-get install -y -qq ffmpeg espeak-ng curl unzip >/dev/null
fi
if ! command -v rclone >/dev/null; then
  curl -sSL https://rclone.org/install.sh | bash >/dev/null 2>&1 || {
    curl -sSLo /tmp/rclone.zip https://downloads.rclone.org/rclone-current-linux-amd64.zip
    unzip -qo /tmp/rclone.zip -d /tmp && cp /tmp/rclone-*/rclone /usr/local/bin/ && chmod +x /usr/local/bin/rclone; }
fi

# --- rclone R2 remote (env-var form; nothing written to disk) ---------------
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="$R2_S3_ENDPOINT"
export RCLONE_CONFIG_R2_ACL=private

# --- python deps -----------------------------------------------------------
pip install -q --no-input kokoro soundfile "transformers<5" 2>&1 | tail -1 || true
python3 -c "import kokoro, torch; print('[boot] torch', torch.__version__, 'cuda', torch.cuda.is_available())"

# --- fetch worker ----------------------------------------------------------
mkdir -p /workspace/narr
rclone copy r2:falsafa-audio/_bootstrap/pod_worker.py /workspace/ --s3-no-check-bucket

# --- launch N worker processes (one GPU can feed several Kokoro streams) ----
PROCS="${PROCS:-2}"
POD_ID="${RUNPOD_POD_ID:-$(hostname)}"
EXTRA="${WORKER_EXTRA:-}"
for i in $(seq 0 $((PROCS-1))); do
  SHARD=$((SHARD_BASE + i))
  echo "[boot] launching shard $SHARD/$SHARDS_TOTAL"
  nohup python3 -u /workspace/pod_worker.py --shard "$SHARD" --of "$SHARDS_TOTAL" \
      --bucket r2:falsafa-audio --workdir "/workspace/narr/w$i" $EXTRA \
      > "/workspace/shard_${SHARD}.log" 2>&1 &
done
echo "[boot] $PROCS workers launched"

# ship logs to R2 every 30s so progress is visible without SSH
( while true; do
    rclone copy /workspace "r2:falsafa-audio/_logs/$POD_ID/" \
      --include "*.log" --s3-no-check-bucket -q 2>/dev/null || true
    sleep 30
  done ) &
LOGGER=$!

wait $(jobs -p | grep -v "$LOGGER" 2>/dev/null) 2>/dev/null || wait
sleep 2
rclone copy /workspace "r2:falsafa-audio/_logs/$POD_ID/" --include "*.log" --s3-no-check-bucket -q || true
echo "[boot] all workers exited"
rclone copy /workspace "r2:falsafa-audio/_logs/$POD_ID/" --include "*.log" --s3-no-check-bucket -q || true
sleep infinity
