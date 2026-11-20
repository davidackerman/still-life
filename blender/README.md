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

To post it in the bot's 8 PM slot (the bot then adopts it instead of posting its own), shortly before 8 PM ET:

    git pull
    python blender/tree_snippet.py /big/disk/tree/snippet.json /big/disk/tree/tree_future.npz
    python blender/upload_short.py /big/disk/tree/tree.mp4 /big/disk/tree/snippet.json

`tree_snippet.py` writes the title and description, ending in the slot marker. `upload_short.py` reads the channel
login from `~/.config/still-life/youtube.env` (CLIENT_ID=, CLIENT_SECRET=, REFRESH_TOKEN= lines, chmod 600). It
never prints the login, and it refuses to upload twice with the same marker. See also "Daily Shorts" in SETUP.md.
