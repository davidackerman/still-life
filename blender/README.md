# Blender Shorts

The 3D Shorts made by hand in Blender 4.2 (not part of the bot). The universe's history is stacked in time,
one layer per sampled generation, and rendered with Cycles.

- `build_tree_future.py`: the data. It replays `state/` and colors each cell by its family (the comment it descends
  from). Then it adds the forecast future if nobody comments, withering, a day of it stuck, and one example
  comment that brings it back.
- `tower_tree.py`: the render (`blender -b --python blender/tower_tree.py -- ...`). It draws a tree on a meadow at
  sunset. After the opening it cuts to the seed in the grass, regrows it, browns it as its future dies, and greens
  it again from the comment. It uses Metal on a Mac and NVIDIA (OptiX, or `TOWER_DEVICE=CUDA`) elsewhere.
- `tower_post.py`: adds the words and sound and encodes the MP4 (`TOWER_WORDS=tree`). It needs ffmpeg.
- `render_tree.sh`: runs all three steps with the settings used on Nov 19, 2026.

A quick speed test, every 30th frame at 30% size:

    STEP=30 PERCENT=30 BLENDER=/path/to/blender-4.2/blender GPUS=1 blender/render_tree.sh /big/disk/tree_test

The whole Short, one Blender process per card:

    BLENDER=/path/to/blender-4.2/blender GPUS=0,1 blender/render_tree.sh /big/disk/tree

The log prints each frame's time (`Time: 00:17.40` is 17.4 s). On a MacBook Pro (M3 Max), two processes take
about 17 s a frame each at full size.

To post a Short in the bot's 8 PM slot, see "Daily Shorts" in SETUP.md.
