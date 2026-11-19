#!/usr/bin/env bash
# The tree-of-life Short: build its data from state/, render it in Blender, then add the words and sound (an MP4).
#
#   blender/render_tree.sh OUTDIR                        the whole Short (586 frames, 1080x1920)
#   STEP=30 PERCENT=30 blender/render_tree.sh OUTDIR     a quick test: every 30th frame, small; no MP4
#
# Env: BLENDER  path to Blender 4.2 (default: blender)
#      GPUS     NVIDIA cards to use, as Blender lists them: "1", or "0,1" for one process per card (alternate frames)
#      FORMAT   PNG (default) or JPEG (a tenth the disk)
#      PYTHON   a Python with numpy and pillow (default: python3); the MP4 also needs ffmpeg
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:?usage: blender/render_tree.sh OUTDIR}
BLENDER=${BLENDER:-blender}
PYTHON=${PYTHON:-python3}
STEP=${STEP:-1}
PERCENT=${PERCENT:-100}
GPUS=${GPUS:-}
FRAMES=586
mkdir -p "$OUT/frames"
OUT=$(cd "$OUT" && pwd)

"$PYTHON" blender/build_tree_future.py "$OUT/tree_future.npz"

# the look and the camera (as rendered on Nov 19, 2026)
export TOWER_SPROUT=75 TOWER_REGROW=105 TOWER_ORBIT=120 TOWER_LAWN=5,12,90,0.2 TOWER_PULL=1.05 \
  TOWER_HOLD0=90 TOWER_HOLD_END=75 TOWER_SUN_OFF=4 TOWER_GRASS_S=2.5 TOWER_OZONE=3 TOWER_LIFT=0.15 \
  TOWER_NOISE=0.03 TOWER_CROWN=1 TOWER_DIST=1.3 TOWER_DUST=0.6 TOWER_AIR=1.0 TOWER_SKY=0.07 \
  TOWER_SUN_EL=2.5 TOWER_GLOW=3 TOWER_EXPOSURE=0.55 TOWER_FORMAT=${FORMAT:-PNG}

run() {  # $1: first frame, $2: frame step, $3: the GPUs for this process
  TOWER_START=$1 TOWER_STEP=$2 TOWER_GPUS=$3 "$BLENDER" -b --python-exit-code 1 --python blender/tower_tree.py -- \
    "$OUT/tree_future.npz" "$OUT/frames/f_" leaf 0.4 32 35 -5 tree round $FRAMES "$PERCENT" > "$OUT/render_$1.log" 2>&1
}
start=$(date +%s)
if [[ "$GPUS" == *,* && "$STEP" == 1 ]]; then
  IFS=, read -r g0 g1 <<< "$GPUS"
  run 1 2 "$g0" &
  run 2 2 "$g1" &
  wait
else
  run 1 "$STEP" "$GPUS"
fi
echo "rendered in $(( ($(date +%s) - start) / 60 )) min; the devices and the last frames' times:"
grep -h "^devices:" "$OUT"/render_*.log | sort -u
grep -h "^Time" "$OUT"/render_*.log | tail -4

if [[ "$STEP" == 1 ]]; then
  TOWER_WORDS=tree "$PYTHON" blender/tower_post.py "$OUT/frames" "$OUT/frames/f__timing.json" "$OUT/tree.mp4" state \
    "$OUT/tree_future.npz"
fi
