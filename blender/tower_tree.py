"""The universe's history stacked in time (generation = height), rendered in Blender.

blender -b -P tower.py -- voxels.npz out.png [plastic|metal|jelly] [layer] [samples] [azimuth] [elev]
"""
import math
import os
import sys

import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:]
SRC, OUT = argv[0], argv[1]
MAT = argv[2] if len(argv) > 2 else "plastic"
LAYER = float(argv[3]) if len(argv) > 3 else 0.4  # height per generation, in cells
SAMPLES = int(argv[4]) if len(argv) > 4 else 96
AZ = math.radians(float(argv[5]) if len(argv) > 5 else 35)
EL = math.radians(float(argv[6]) if len(argv) > 6 else 18)
GAP = 0.12  # between neighbouring cells (in time they touch: tubes)
COLOR_MODE = argv[7] if len(argv) > 7 else "time"  # time: uncolored cells shift with time; pearl: they're white
TIME_COLOR = COLOR_MODE == "time"
ROUND = (argv[8] if len(argv) > 8 else "round") == "round"  # the cubes' edges rounded a little
FRAMES = int(argv[9]) if len(argv) > 9 else 0  # 0: a still; else a video: it grows from the bottom up, the camera circling
PERCENT = int(argv[10]) if len(argv) > 10 else 100
import os
START_FRAME = int(os.environ.get("TOWER_START", "1"))  # resume a render from this frame
CROP = int(argv[11]) if len(argv) > 11 else 0  # a CROP x CROP window of the board, where the most moves landed
DIVE_SCALE, DIVE_Z, DIVE_AT = (0.07, 0.4), (0.01, 0.2), 0.3  # close on the first cells, then pulled back this far by DIVE_AT of the growth
FLY = os.environ.get("TOWER_FLY", "") == "1"  # fly up through the finished tower instead (no growing), out of its top
FLY_TEST = os.environ.get("TOWER_FLY_TEST", "")  # "0.1,0.5,0.9": stills at these fractions of the flight
FLY_EXIT = 0.8  # of the frames: inside until here, then out of the top, looking down at today
FLY_TILT, FLY_YAW, FLY_LENS = math.radians(float(os.environ.get("TOWER_FLY_TILT", "14"))), math.radians(110), float(os.environ.get("TOWER_FLY_LENS", "20"))
HOLD0, HOLD_END, ORBIT = int(os.environ.get("TOWER_HOLD0", "30")), int(os.environ.get("TOWER_HOLD_END", "75")), float(os.environ.get("TOWER_ORBIT", "360"))  # frames on the finished tower first and last; the camera goes all the way round (it loops)

d = np.load(SRC)
H, W = (int(v) for v in d["shape"])
x, y, t = d["x"].astype(np.float32), d["y"].astype(np.float32), d["t"].astype(np.float32)
col = d["c"].astype(np.float32) / 255.0
FUT_L, FROZEN_L, INJ_L = (int(d["fut"]), int(d["frozen"]), int(d["inject"])) if "fut" in d.files else (None, None, None)
has_moves = "mx" in d.files and len(d["mx"])
mx_, my_, mt_, mc_ = ((d["mx"].astype(np.float32), d["my"].astype(np.float32), d["mt"].astype(np.float32),
                       d["mc"].astype(np.float32) / 255.0) if has_moves else (np.zeros(0),) * 3 + (np.zeros((0, 3)),))
if CROP:  # the window (on the torus) holding the most moves' cells
    m = np.zeros((H, W))
    np.add.at(m, (my_.astype(int), mx_.astype(int)), 1)
    S = min(CROP, H)
    _, ox, oy = max(((np.roll(np.roll(m, -oy, 0), -ox, 1)[:S, :CROP].sum(), ox, oy) for oy in range(H) for ox in range(W)))

    def inside(a, b):
        return ((a - ox) % W < CROP) & ((b - oy) % H < S)
    keep = inside(x, y)
    x, y, t, col = (x[keep] - ox) % W - CROP / 2, (y[keep] - oy) % H - S / 2, t[keep], col[keep]
    km = inside(mx_, my_)
    mx_, my_, mt_, mc_ = (mx_[km] - ox) % W - CROP / 2, (my_[km] - oy) % H - S / 2, mt_[km], mc_[km]
else:  # centre on where it began (the first layer), the board unwrapped around it (it's a torus)
    first = t == t.min()
    cx, cy = float(x[first].mean()) + 0.5, float(y[first].mean()) + 0.5
    x = (x - cx + W / 2) % W - W / 2
    y = (y - cy + H / 2) % H - H / 2
    mx_ = (mx_ - cx + W / 2) % W - W / 2
    my_ = (my_ - cy + H / 2) % H - H / 2
CROWN = os.environ.get("TOWER_CROWN", "0") == "1"
if CROWN:  # a tree's outline: the board's corners cut off (an ellipse), the top domed; the trunk untouched
    T = float(FUT_L) if FUT_L else max(1.0, float(t.max()))
    TOP_T = max(1.0, float(t.max()))
    D0, D1 = (float(v) for v in os.environ.get("TOWER_DEAD_R", "0.5,0.3").split(","))
    def crowned(a, b, tt):
        k = tt / T
        r = np.sqrt(np.clip(1 - ((k - 0.58) / 0.5).clip(0, None) ** 2, 0, 1))
        if FUT_L:  # the future: a narrowing dead top (dieback); what the comment brings back opens out again
            dead = D0 + (D1 - D0) * np.clip((tt - FUT_L) / max(1, INJ_L - FUT_L), 0, 1)
            back = D1 + (D0 - D1) * 0.6 * np.clip((tt - INJ_L) / max(1.0, TOP_T - INJ_L), 0, 1)
            r = np.where(tt >= INJ_L, back, np.where(tt >= FUT_L, dead, r))
        return (a / (W / 2)) ** 2 + (b / (H / 2)) ** 2 <= r ** 2
    keep = crowned(x, y, t)
    x, y, t, col = x[keep], y[keep], t[keep], col[keep]
    km = crowned(mx_, my_, mt_)
    mx_, my_, mt_, mc_ = mx_[km], my_[km], mt_[km], mc_[km]
    print(f"crown: kept {keep.mean():.0%} of the voxels", flush=True)
# runs: a cell alive (and the same color) for consecutive generations becomes one tall piece
painted = ~np.all(np.isclose(col, np.median(col, 0)), 1)
order = np.lexsort((t, y, x))
x, y, t, col, painted = x[order], y[order], t[order], col[order], painted[order]
new_run = np.ones(len(x), bool)
new_run[1:] = (x[1:] != x[:-1]) | (y[1:] != y[:-1]) | (t[1:] != t[:-1] + 1) | (np.abs(col[1:] - col[:-1]).sum(1) > 1e-3)
starts = np.nonzero(new_run)[0]
lengths = np.diff(np.append(starts, len(x)))
x, y, t, col, painted = x[starts], y[starts], t[starts], col[starts], painted[starts]
n = len(x)
# color: the cells' own (a comment's color); uncolored ones shift with time, cool at the bottom to warm at the top
if TIME_COLOR:
    k = (t + lengths / 2) / max(1.0, (t + lengths).max())
    stops = np.array([[0.20, 0.85, 0.80], [0.35, 0.55, 1.0], [0.75, 0.45, 1.0], [1.0, 0.55, 0.65], [1.0, 0.80, 0.45]], np.float32)
    pos = k * (len(stops) - 1)
    i0 = np.minimum(pos.astype(int), len(stops) - 2)
    fr = (pos - i0)[:, None]
    grad = stops[i0] * (1 - fr) + stops[i0 + 1] * fr
    col = np.where(painted[:, None], col, grad).astype(np.float32)
elif COLOR_MODE == "tree":  # long-lived cells (still lifes, standing for many layers) are wood; the rest leaves
    rng = np.random.default_rng(3)
    WOOD = int(os.environ.get("TOWER_WOOD", "6"))
    wood = (lengths >= WOOD) & ((t < FUT_L) if FUT_L else True)  # the future keeps its own colors (dying, dead)
    bark = np.array([0.27, 0.17, 0.09], np.float32) + rng.random((n, 1)).astype(np.float32) * np.array([0.14, 0.11, 0.08], np.float32)
    leaf = col * rng.uniform(0.72, 1.12, (n, 1)).astype(np.float32)  # each leaf a little lighter or darker
    leaf[:, 1] *= rng.uniform(0.92, 1.06, n).astype(np.float32)
    col = np.clip(np.where(wood[:, None], bark, leaf), 0, 1).astype(np.float32)
    print(f"wood: {wood.mean():.1%} of the pieces", flush=True)
    # each comment's cells, where they landed: a blossom (pinks, creams, a soft gold)
    bl = np.array([[0.95, 0.62, 0.68], [0.98, 0.90, 0.82], [0.96, 0.78, 0.45], [0.88, 0.50, 0.60]], np.float32)
    mc_ = bl[rng.integers(0, len(bl), len(mc_))] if len(mc_) else mc_
    if INJ_L is not None and len(mc_):
        mc_[mt_ == INJ_L] = (1.0, 0.92, 0.72)  # the one comment that brings it back: bright
elif COLOR_MODE == "pearl":  # only what people painted is colored
    col = np.where(painted[:, None], col, np.array([0.86, 0.85, 0.88], np.float32)).astype(np.float32)
print(f"{len(order)} voxels in {n} pieces", flush=True)

# ---- scene
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene

# ---- one mesh of n boxes, built with numpy
quads = np.array([[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]], np.int32)


def boxes(name, x, y, t, lengths, col, gap):
    # one hidden box under the floor, never deleted: an object that empties out crashes the renderer
    x, y = np.append(x, x[0] if len(x) else 0), np.append(y, y[0] if len(y) else 0)
    t, lengths = np.append(t, -40.0 / LAYER), np.append(lengths, 1)
    col = np.concatenate([col, np.zeros((1, 3), np.float32)])
    n = len(x)
    s = 0.5 - gap / 2
    corner = np.array([[-s, -s, 0], [s, -s, 0], [s, s, 0], [-s, s, 0],
                       [-s, -s, 1], [s, -s, 1], [s, s, 1], [-s, s, 1]], np.float32)
    centre = np.stack([x + 0.5, -(y + 0.5), t * LAYER], 1)
    verts = centre[:, None, :] + corner[None] * np.stack([np.ones(n), np.ones(n), lengths * LAYER], 1)[:, None, :].astype(np.float32)
    faces = (quads[None] + 8 * np.arange(n, dtype=np.int32)[:, None, None]).reshape(-1, 4)
    me = bpy.data.meshes.new(name)
    me.vertices.add(8 * n)
    me.vertices.foreach_set("co", verts.reshape(-1))
    me.loops.add(24 * n)
    me.loops.foreach_set("vertex_index", faces.reshape(-1))
    me.polygons.add(6 * n)
    me.polygons.foreach_set("loop_start", np.arange(0, 24 * n, 4, dtype=np.int32))
    me.polygons.foreach_set("loop_total", np.full(6 * n, 4, np.int32))
    ca = me.color_attributes.new("col", "FLOAT_COLOR", "FACE")
    rgba = np.concatenate([col, np.ones((n, 1), np.float32)], 1) ** np.array([2.2, 2.2, 2.2, 1], np.float32)  # sRGB -> linear
    ca.data.foreach_set("color", np.repeat(rgba, 6, 0).reshape(-1))
    t0 = me.attributes.new("t0", "FLOAT", "POINT")
    t0.data.foreach_set("value", np.repeat(t * LAYER, 8).astype(np.float32))
    me.update()
    me.validate()
    o = bpy.data.objects.new(name, me)
    sc.collection.objects.link(o)
    return o, me


STRAND = float(os.environ.get("TOWER_STRAND", "0"))  # >0: each cell a thin strand this wide (room to see through it)
JITTER = float(os.environ.get("TOWER_JITTER", "0"))  # each column's strand nudged up to this far off the grid


def nudge(a, b):  # (dx, dy) for the columns at (a, b): fixed per column, so a strand stays straight
    ia, ib = np.floor(a + 1000).astype(np.int64), np.floor(b + 1000).astype(np.int64)
    h1 = (ia * 73856093) ^ (ib * 19349663)
    h2 = (ia * 83492791) ^ (ib * 2654435761)
    return ((h1 % 1009) / 1008.0 - 0.5) * 2 * JITTER, ((h2 % 1013) / 1012.0 - 0.5) * 2 * JITTER


if JITTER:
    jx, jy = nudge(x, y)
    mjx, mjy = nudge(mx_, my_)
else:
    jx = jy = mjx = mjy = 0.0
ob, me = boxes("tower", x + jx, y + jy, t, lengths, col, 1 - STRAND if STRAND else GAP)
sparks = boxes("moves", mx_ + mjx, my_ + mjy, mt_, (np.where(mt_ == INJ_L, 5.0, 1.6) if INJ_L is not None else np.ones(len(mx_)) * 1.6), mc_, 1 - min(1.15, STRAND * 1.8) if STRAND else -0.15)[0] if len(mx_) else None
if ROUND:  # rounded once, here, not every frame
    bev = ob.modifiers.new("round", "BEVEL")
    bev.width, bev.segments, bev.harden_normals = (min(0.09, STRAND * 0.3) if STRAND else 0.09), 2, True
    me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), bool))
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.modifier_apply(modifier="round")
    me = ob.data
top = float((t + lengths).max() * LAYER)
AHEAD = float(os.environ.get("TOWER_AHEAD", "0"))  # flying: the tower exists only up to this far above the camera
if FRAMES and (not FLY or AHEAD):  # growing: pieces above the growth front aren't there yet, the ones it's passing through are cut at it
    ng = bpy.data.node_groups.new("grow", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Grow", in_out="INPUT", socket_type="NodeSocketFloat")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    N, L = ng.nodes.new, ng.links.new
    gi, go = N("NodeGroupInput"), N("NodeGroupOutput")
    na = N("GeometryNodeInputNamedAttribute")
    na.data_type = "FLOAT"
    na.inputs["Name"].default_value = "t0"
    cmp = N("FunctionNodeCompare")
    cmp.data_type, cmp.operation = "FLOAT", "GREATER_EQUAL"
    L(na.outputs["Attribute"], cmp.inputs["A"])
    L(gi.outputs["Grow"], cmp.inputs["B"])
    dele = N("GeometryNodeDeleteGeometry")
    dele.domain = "POINT"
    L(gi.outputs["Geometry"], dele.inputs["Geometry"])
    L(cmp.outputs["Result"], dele.inputs["Selection"])
    pos, sep, mn, comb = N("GeometryNodeInputPosition"), N("ShaderNodeSeparateXYZ"), N("ShaderNodeMath"), N("ShaderNodeCombineXYZ")
    mn.operation = "MINIMUM"
    L(pos.outputs["Position"], sep.inputs["Vector"])
    L(sep.outputs["Z"], mn.inputs[0])
    L(gi.outputs["Grow"], mn.inputs[1])
    L(sep.outputs["X"], comb.inputs["X"])
    L(sep.outputs["Y"], comb.inputs["Y"])
    L(mn.outputs["Value"], comb.inputs["Z"])
    setp = N("GeometryNodeSetPosition")
    L(dele.outputs["Geometry"], setp.inputs["Geometry"])
    L(comb.outputs["Vector"], setp.inputs["Position"])
    L(setp.outputs["Geometry"], go.inputs["Geometry"])
    G0, GF = HOLD0 + 1, FRAMES - HOLD_END  # it rebuilds from the bottom between these frames
    key = ng.interface.items_tree["Grow"].identifier
    full = top + LAYER
    NOW_H = FUT_L * LAYER if FUT_L else 0.0
    SPROUT, REGROW = int(os.environ.get("TOWER_SPROUT", "0")), int(os.environ.get("TOWER_REGROW", "0"))
    G1 = G0 + SPROUT + REGROW if (FUT_L and SPROUT) else G0  # the tree to now, regrown from its seed, between G0 and G1
    DENSE_H = min(120, FUT_L or 120) * LAYER  # the first 120 generations are a layer each: the sprouting
    GROW_KEYS = (((1, NOW_H), (HOLD0, NOW_H), (G0, LAYER), (G0 + SPROUT, DENSE_H), (G1, NOW_H), (GF, full), (FRAMES, full))
                 if FUT_L and SPROUT else
                 ((1, NOW_H), (G0, NOW_H), (GF, full), (FRAMES, full)) if FUT_L else
                 ((1, full), (HOLD0, full), (G0, 0.0), (GF, full), (FRAMES, full)))
    grow_mods = []
    for o in ([] if FLY else [ob] + ([sparks] if sparks else [])):
        gm = o.modifiers.new("grow", "NODES")
        gm.node_group = ng
        for f, v in GROW_KEYS:
            gm[key] = v
            gm.keyframe_insert(data_path=f'["{key}"]', frame=f)
        for fc in o.animation_data.action.fcurves:  # steady, so the chimes can be timed to it
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
    for o in ([ob] + ([sparks] if sparks else [])) if FLY else []:
        gm = o.modifiers.new("grow", "NODES")
        gm.node_group = ng
        grow_mods.append(gm)
    import json
    if not FLY:
      json.dump({"G0": G0, "GF": GF, "frames": FRAMES, "full": full, "layer": LAYER, "from": NOW_H, "G1": G1,
               "keys": [[float(f), float(v)] for f, v in GROW_KEYS],
               "moves": [[float(a), [float(v) for v in c], float(px)] for a, c, px in zip(mt_, mc_, mx_)]},
              open(OUT.rstrip("/f") + "_timing.json", "w"))
FLY_N = int(os.environ.get("TOWER_FLY_N", "0")) or FRAMES  # the flight's own frames; after them, the swoop back
if FLY:  # the flight's timing, for the words and the sound: the layer it has reached, frame by frame
    import json
    GX = max(2, int(FLY_N * FLY_EXIT))
    json.dump({"G0": 1, "GF": GX, "frames": FRAMES, "full": top + LAYER, "layer": LAYER, "fly": True, "fly_n": FLY_N},
              open(OUT.rstrip("/f") + "_timing.json", "w"))
    # a way up through the middle: the emptiest 2x2 of columns near where it began (the camera sits in the gap
    # between four columns' cells, so it never passes through one)
    fx0, fy0 = float(x[0] - math.floor(x[0])), float(y[0] - math.floor(y[0]))  # the grid's offset (centred on the seed)
    occ = {}
    for xx, yy, ll in zip(np.round(x - fx0).astype(int), np.round(y - fy0).astype(int), lengths):
        occ[(xx, yy)] = occ.get((xx, yy), 0) + int(ll)
    best = min(((sum(occ.get((a + i, b + j), 0) for i in (-1, 0) for j in (-1, 0)) + 2 * (abs(a) + abs(b)), a, b)
                for a in range(-6, 7) for b in range(-6, 7)))
    FX, FY = best[1] + fx0, -(best[2] + fy0)  # the corner where four columns meet: in the gap between their cells
    print("flying up at", FX, FY, "occupancy", best[0], flush=True)
# ---- materials
def principled(name):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    return m, m.node_tree.nodes["Principled BSDF"], m.node_tree
mat, bsdf, nt = principled("blocks")
attr = nt.nodes.new("ShaderNodeVertexColor")
attr.layer_name = "col"
nt.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
if MAT == "metal":
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.28
elif MAT == "jelly":
    bsdf.inputs["Transmission Weight"].default_value = 0.85
    bsdf.inputs["Roughness"].default_value = 0.08
    bsdf.inputs["IOR"].default_value = 1.33
    bsdf.inputs["Subsurface Weight"].default_value = 0.3
    nt.links.new(attr.outputs["Color"], bsdf.inputs["Subsurface Radius"])
elif MAT == "leaf":  # matte, a little translucent, like leaves and bark
    bsdf.inputs["Roughness"].default_value = 0.62
    bsdf.inputs["Specular IOR Level"].default_value = 0.3
    bsdf.inputs["Subsurface Weight"].default_value = 0.12
    bsdf.inputs["Sheen Weight"].default_value = 0.25
else:  # glossy candy plastic
    bsdf.inputs["Roughness"].default_value = 0.32
    bsdf.inputs["Coat Weight"].default_value = 0.8
    bsdf.inputs["Coat Roughness"].default_value = 0.05
FRONTS = []  # (death front, life front) Value nodes, one pair per material, keyed below
SOFT = float(os.environ.get("TOWER_SOFT", "30"))  # how soft the fronts' edges are


def dead_fac(nt_):  # 1 where the tree to now has died (above the death front, not yet below the life front)
    NN, LL = nt_.nodes.new, nt_.links.new
    ta = NN("ShaderNodeAttribute")
    ta.attribute_name = "t0"
    hist = NN("ShaderNodeMath")
    hist.operation = "LESS_THAN"
    hist.inputs[1].default_value = FUT_L * LAYER - 0.01
    LL(ta.outputs["Fac"], hist.inputs[0])
    out_ = hist.outputs[0]
    pair = []
    for sign in (1, -1):  # death: (t0 - DF)/SOFT; life back: 1 - (t0 - LF)/SOFT
        v = NN("ShaderNodeValue")
        v.outputs[0].default_value = FUT_L * LAYER + SOFT if sign == 1 else FUT_L * LAYER + SOFT
        sub, div = NN("ShaderNodeMath"), NN("ShaderNodeMath")
        sub.operation, div.operation = "SUBTRACT", "DIVIDE"
        LL(ta.outputs["Fac"], sub.inputs[0])
        LL(v.outputs[0], sub.inputs[1])
        LL(sub.outputs[0], div.inputs[0])
        div.inputs[1].default_value = SOFT
        div.use_clamp = True
        term = div.outputs[0]
        if sign == -1:
            inv = NN("ShaderNodeMath")
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            LL(term, inv.inputs[1])
            term = inv.outputs[0]
        mul = NN("ShaderNodeMath")
        mul.operation = "MULTIPLY"
        LL(out_, mul.inputs[0])
        LL(term, mul.inputs[1])
        out_ = mul.outputs[0]
        pair.append(v)
    FRONTS.append(tuple(pair))
    return out_


if FUT_L and COLOR_MODE == "tree":  # the tree to now browns and dries where it has died
    NN, LL = nt.nodes.new, nt.links.new
    dfac = dead_fac(nt)
    sat, val, dry_f = NN("ShaderNodeMath"), NN("ShaderNodeMath"), NN("ShaderNodeMath")
    for m_, a_, b_ in ((sat, -float(os.environ.get("TOWER_DESAT", "0.45")), 1.0), (val, -0.25, 1.0),
                       (dry_f, float(os.environ.get("TOWER_DRY", "0.7")), 0.0)):
        m_.operation = "MULTIPLY_ADD"
        m_.inputs[1].default_value, m_.inputs[2].default_value = a_, b_
        LL(dfac, m_.inputs[0])
    hs = NN("ShaderNodeHueSaturation")
    LL(attr.outputs["Color"], hs.inputs["Color"])
    LL(sat.outputs[0], hs.inputs["Saturation"])
    LL(val.outputs[0], hs.inputs["Value"])
    dry = NN("ShaderNodeMixRGB")
    dry.inputs["Color2"].default_value = tuple(float(v) for v in os.environ.get("TOWER_DRY_C", "0.20,0.11,0.045").split(",")) + (1.0,)
    LL(dry_f.outputs[0], dry.inputs["Fac"])
    LL(hs.outputs["Color"], dry.inputs["Color1"])
    LL(dry.outputs["Color"], bsdf.inputs["Base Color"])
me.materials.append(mat)
if sparks:  # each move's cells, where it landed: lit from inside
    sm = bpy.data.materials.new("moves")
    sm.use_nodes = True
    snt = sm.node_tree
    em = snt.nodes.new("ShaderNodeEmission")
    sa = snt.nodes.new("ShaderNodeVertexColor")
    sa.layer_name = "col"
    snt.links.new(sa.outputs["Color"], em.inputs["Color"])
    em.inputs["Strength"].default_value = float(os.environ.get("TOWER_GLOW", "12"))
    snt.links.new(em.outputs["Emission"], snt.nodes["Material Output"].inputs["Surface"])
    if FUT_L and COLOR_MODE == "tree":
        sf = dead_fac(snt)
        sk = snt.nodes.new("ShaderNodeMath")
        sk.operation = "MULTIPLY_ADD"
        sk.inputs[1].default_value, sk.inputs[2].default_value = -float(os.environ.get("TOWER_GLOW", "12")) * 0.95, float(os.environ.get("TOWER_GLOW", "12"))
        snt.links.new(sf, sk.inputs[0])
        snt.links.new(sk.outputs[0], em.inputs["Strength"])
    sparks.data.materials.append(sm)
    sparks.modifiers.new("round", "BEVEL").width = 0.06

# a glossy dark floor
bpy.ops.mesh.primitive_plane_add(size=200000 if COLOR_MODE == "tree" else 4000, location=(0, 0, 0))  # tree: out to the horizon
floor = bpy.context.active_object
fm, fb, _ = principled("floor")
fb.inputs["Base Color"].default_value = (0.014, 0.011, 0.008, 1) if COLOR_MODE == "tree" else (0.008, 0.009, 0.02, 1)  # tree: earth
fb.inputs["Roughness"].default_value = 0.55 if COLOR_MODE == "tree" else 0.3
fb.inputs["Specular IOR Level"].default_value = 0.25  # reflections, without a glare of the key light
floor.data.materials.append(fm)

# ---- world and lights: a dark navy studio, a big soft key, a cool rim
world = bpy.data.worlds.new("w")
sc.world = world
world.use_nodes = True
bg = world.node_tree.nodes["Background"]
bg.inputs["Color"].default_value = (0.010, 0.010, 0.016, 1) if COLOR_MODE == "tree" else (0.006, 0.008, 0.02, 1)
bg.inputs["Strength"].default_value = 1.0

span = float(max(x.max() - x.min(), y.max() - y.min(), top / 1.6))
def area(name, loc, energy, size, color):
    L = bpy.data.lights.new(name, "AREA")
    L.energy, L.size, L.color = energy, size, color
    o = bpy.data.objects.new(name, L)
    o.location = loc
    sc.collection.objects.link(o)
    c = o.constraints.new("TRACK_TO")
    c.target = target
    return o
mx, my = float((x.min() + x.max()) / 2 + 0.5), float(-(y.min() + y.max()) / 2 - 0.5)
NATURE = COLOR_MODE == "tree" and os.environ.get("TOWER_NATURE", "1") == "1"
if NATURE:  # a meadow at sunset: a sky with the sun low behind the tree, a sun lamp to match, grass on the ground
    from mathutils import Vector
    SUN_EL = math.radians(float(os.environ.get("TOWER_SUN_EL", "5")))
    SUN_AZ = AZ + math.radians(float(os.environ.get("TOWER_SUN_OFF", "28")))  # behind the tree, a little to one side
    sun_dir = Vector((math.sin(SUN_AZ) * math.cos(SUN_EL), math.cos(SUN_AZ) * math.cos(SUN_EL), math.sin(SUN_EL)))
    wn, wl = world.node_tree.nodes, world.node_tree.links
    sky = wn.new("ShaderNodeTexSky")
    sky.sky_type = "NISHITA"
    sky.sun_elevation = SUN_EL
    sky.sun_rotation = float(os.environ.get("TOWER_SUN_ROT", str(math.atan2(sun_dir.x, sun_dir.y))))
    sky.sun_disc = True
    sky.sun_size = math.radians(float(os.environ.get("TOWER_SUN_SIZE", "0.9")))
    sky.air_density = float(os.environ.get("TOWER_AIR", "1.4"))
    sky.dust_density = float(os.environ.get("TOWER_DUST", "4.0"))  # hazy: a golden horizon
    sky.ozone_density = float(os.environ.get("TOWER_OZONE", "1.0"))  # more: a bluer sky overhead
    wl.new(sky.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = float(os.environ.get("TOWER_SKY", "0.35"))
    sl = bpy.data.lights.new("sun", "SUN")
    sl.energy, sl.color, sl.angle = float(os.environ.get("TOWER_SUN", "3.5")), (1.0, 0.60, 0.34), math.radians(1.5)
    so = bpy.data.objects.new("sun", sl)
    sc.collection.objects.link(so)
    so.rotation_euler = sun_dir.to_track_quat("Z", "Y").to_euler()
    # the ground: earth and grass, mottled
    fnt = fm.node_tree
    for nd in list(fnt.nodes):
        if nd.type == "TEX_COORD" or nd.type == "TEX_NOISE":
            fnt.nodes.remove(nd)
    tc, nz, ramp, bump = (fnt.nodes.new(k) for k in ("ShaderNodeTexCoord", "ShaderNodeTexNoise", "ShaderNodeValToRGB", "ShaderNodeBump"))
    nz.inputs["Scale"].default_value, nz.inputs["Detail"].default_value = 0.06, 8.0
    fnt.links.new(tc.outputs["Object"], nz.inputs["Vector"])
    fnt.links.new(nz.outputs["Fac"], ramp.inputs["Fac"])
    e0, e1 = ramp.color_ramp.elements
    e0.position, e0.color = 0.35, (0.045, 0.085, 0.018, 1)
    e1.position, e1.color = 0.70, (0.120, 0.150, 0.035, 1)
    fnt.links.new(ramp.outputs["Color"], fb.inputs["Base Color"])
    fb.inputs["Roughness"].default_value, fb.inputs["Specular IOR Level"].default_value = 0.95, 0.0
    bump.inputs["Strength"].default_value = 0.3
    fnt.links.new(nz.outputs["Fac"], bump.inputs["Height"])
    fnt.links.new(bump.outputs["Normal"], fb.inputs["Normal"])
    # grass: blades scattered on a disc around the tree (instances: cheap to render)
    ng2 = bpy.data.node_groups.new("grass", "GeometryNodeTree")
    ng2.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng2.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    N2, L2 = ng2.nodes.new, ng2.links.new
    gout = N2("NodeGroupOutput")
    circ = N2("GeometryNodeMeshCircle")
    circ.fill_type = "NGON"
    circ.inputs["Vertices"].default_value = 96
    circ.inputs["Radius"].default_value = span * float(os.environ.get("TOWER_GRASS_R", "2.4"))
    tri = N2("GeometryNodeTriangulate")
    L2(circ.outputs["Mesh"], tri.inputs["Mesh"])
    dist = N2("GeometryNodeDistributePointsOnFaces")
    dist.inputs["Density"].default_value = float(os.environ.get("TOWER_GRASS_D", "3.0"))
    L2(tri.outputs["Mesh"], dist.inputs["Mesh"])
    gpos, glen, gmap = N2("GeometryNodeInputPosition"), N2("ShaderNodeVectorMath"), N2("ShaderNodeMapRange")
    glen.operation = "LENGTH"
    L2(gpos.outputs["Position"], glen.inputs[0])
    R_G = circ.inputs["Radius"].default_value
    gmap.inputs["From Min"].default_value, gmap.inputs["From Max"].default_value = R_G * 0.55, R_G
    gmap.inputs["To Min"].default_value, gmap.inputs["To Max"].default_value = 1.0, 0.0
    L2(glen.outputs["Value"], gmap.inputs["Value"])
    thin_r, thin_c, thin = N2("FunctionNodeRandomValue"), N2("FunctionNodeCompare"), N2("GeometryNodeDeleteGeometry")
    thin_r.data_type = "FLOAT"
    thin_r.inputs["Seed"].default_value = 9
    thin_c.data_type, thin_c.operation = "FLOAT", "GREATER_THAN"
    L2(thin_r.outputs[1], thin_c.inputs["A"])
    L2(gmap.outputs["Result"], thin_c.inputs["B"])
    thin.domain = "POINT"
    L2(dist.outputs["Points"], thin.inputs["Geometry"])
    LAWN = [float(v) for v in os.environ.get("TOWER_LAWN", "0,0,1,1").split(",")]  # bare radius, short to, full from, short height
    bare_c, bare_or = N2("FunctionNodeCompare"), N2("FunctionNodeBooleanMath")
    bare_c.data_type, bare_c.operation = "FLOAT", "LESS_THAN"
    L2(glen.outputs["Value"], bare_c.inputs["A"])
    bare_c.inputs["B"].default_value = LAWN[0]
    bare_or.operation = "OR"
    L2(thin_c.outputs["Result"], bare_or.inputs[0])
    L2(bare_c.outputs["Result"], bare_or.inputs[1])
    L2(bare_or.outputs[0], thin.inputs["Selection"])
    cone = N2("GeometryNodeMeshCone")
    cone.inputs["Vertices"].default_value = 3
    cone.inputs["Radius Top"].default_value, cone.inputs["Radius Bottom"].default_value = 0.0, 0.14
    cone.inputs["Depth"].default_value = 1.0
    lift = N2("GeometryNodeTransform")
    lift.inputs["Translation"].default_value = (0, 0, 0.5)
    L2(cone.outputs["Mesh"], lift.inputs["Geometry"])
    rr = N2("FunctionNodeRandomValue")
    rr.data_type = "FLOAT_VECTOR"
    rr.inputs[0].default_value, rr.inputs[1].default_value = (-0.45, -0.45, 0.0), (0.45, 0.45, 6.283)
    rh, rw, rc = N2("FunctionNodeRandomValue"), N2("FunctionNodeRandomValue"), N2("FunctionNodeRandomValue")
    for r_, lo, hi, seed in ((rh, 0.5, 1.7, 1), (rw, 0.6, 1.2, 2), (rc, 0.0, 1.0, 3)):
        r_.data_type = "FLOAT"
        r_.inputs["Min"].default_value, r_.inputs["Max"].default_value = lo, hi
        r_.inputs["Seed"].default_value = seed
    comb2 = N2("ShaderNodeCombineXYZ")
    L2(rw.outputs[1], comb2.inputs["X"])
    L2(rw.outputs[1], comb2.inputs["Y"])
    lawn_m, lawn_x = N2("ShaderNodeMapRange"), N2("ShaderNodeMath")
    lawn_m.inputs["From Min"].default_value, lawn_m.inputs["From Max"].default_value = LAWN[1], LAWN[2]
    lawn_m.inputs["To Min"].default_value, lawn_m.inputs["To Max"].default_value = LAWN[3], 1.0
    L2(glen.outputs["Value"], lawn_m.inputs["Value"])
    lawn_x.operation = "MULTIPLY"
    L2(rh.outputs[1], lawn_x.inputs[0])
    L2(lawn_m.outputs["Result"], lawn_x.inputs[1])
    L2(lawn_x.outputs[0], comb2.inputs["Z"])
    inst = N2("GeometryNodeInstanceOnPoints")
    L2(thin.outputs["Geometry"], inst.inputs["Points"])
    gmat, gb, gnt = principled("grass")
    setm = N2("GeometryNodeSetMaterial")
    setm.inputs["Material"].default_value = gmat
    L2(lift.outputs["Geometry"], setm.inputs["Geometry"])
    L2(setm.outputs["Geometry"], inst.inputs["Instance"])
    L2(rr.outputs[0], inst.inputs["Rotation"])
    L2(comb2.outputs["Vector"], inst.inputs["Scale"])
    store = N2("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT", "INSTANCE"
    store.inputs["Name"].default_value = "gcol"
    L2(inst.outputs["Instances"], store.inputs["Geometry"])
    L2(rc.outputs[1], store.inputs["Value"])
    L2(store.outputs["Geometry"], gout.inputs["Geometry"])
    meadow = bpy.data.objects.new("meadow", bpy.data.meshes.new("meadow"))
    sc.collection.objects.link(meadow)
    meadow.location = (0.5, -0.5, 0.0)
    gmod = meadow.modifiers.new("grass", "NODES")
    gmod.node_group = ng2
    ga, gr = gnt.nodes.new("ShaderNodeAttribute"), gnt.nodes.new("ShaderNodeValToRGB")
    ga.attribute_type, ga.attribute_name = "INSTANCER", "gcol"
    gnt.links.new(ga.outputs["Fac"], gr.inputs["Fac"])
    g0, g1 = gr.color_ramp.elements
    g0.color, g1.color = (0.030, 0.090, 0.012, 1), (0.150, 0.180, 0.035, 1)
    gr.color_ramp.elements.new(0.85).color = (0.28, 0.24, 0.07, 1)  # a few dry ones
    gnt.links.new(gr.outputs["Color"], gb.inputs["Base Color"])
    gb.inputs["Roughness"].default_value, gb.inputs["Subsurface Weight"].default_value = 0.6, 0.15
    meadow.data.materials.append(gmat)
    # far off, the ground fades into the sky at the horizon: the same sky, looked at level, in the ground's place
    FN, FL = fnt.nodes.new, fnt.links.new
    fgeo, fneg, fadd, fnrm = FN("ShaderNodeNewGeometry"), FN("ShaderNodeVectorMath"), FN("ShaderNodeVectorMath"), FN("ShaderNodeVectorMath")
    fneg.operation, fadd.operation, fnrm.operation = "MULTIPLY", "ADD", "NORMALIZE"
    fneg.inputs[1].default_value, fadd.inputs[1].default_value = (-1.0, -1.0, 0.0), (0.0, 0.0, float(os.environ.get("TOWER_HZ_UP", "0.02")))
    FL(fgeo.outputs["Incoming"], fneg.inputs[0])
    FL(fneg.outputs["Vector"], fadd.inputs[0])
    FL(fadd.outputs["Vector"], fnrm.inputs[0])
    fsky = FN("ShaderNodeTexSky")
    for a_ in ("sky_type", "sun_elevation", "sun_rotation", "sun_size", "air_density", "dust_density", "ozone_density"):
        setattr(fsky, a_, getattr(sky, a_))
    fsky.sun_disc = False
    FL(fnrm.outputs["Vector"], fsky.inputs["Vector"])
    fem = FN("ShaderNodeEmission")
    FL(fsky.outputs["Color"], fem.inputs["Color"])
    fem.inputs["Strength"].default_value = bg.inputs["Strength"].default_value
    fcam, fmr, fmix = FN("ShaderNodeCameraData"), FN("ShaderNodeMapRange"), FN("ShaderNodeMixShader")
    fmr.interpolation_type = "SMOOTHSTEP"
    FL(fcam.outputs["View Distance"], fmr.inputs["Value"])
    fout = fnt.nodes["Material Output"]
    FL(fout.inputs["Surface"].links[0].from_socket, fmix.inputs[1])
    FL(fem.outputs["Emission"], fmix.inputs[2])
    FL(fmr.outputs["Result"], fmix.inputs["Fac"])
    FL(fmix.outputs["Shader"], fout.inputs["Surface"])
target = bpy.data.objects.new("target", None)
target.location = (mx, my, top * 0.48)
sc.collection.objects.link(target)
R = span * 2.2
area("key", (mx - R * 0.9, my - R * 0.7, top * 1.1), 2.2e5 * (span / 60) ** 2, span * 2.0, (1.0, 0.82, 0.62) if COLOR_MODE == "tree" else (1.0, 0.96, 0.9))  # tree: late sun
area("rim", (mx + R * 0.8, my + R * 0.9, top * 0.8), (0.5 if NATURE else 1.0) * 1.6e5 * (span / 60) ** 2, span * 1.2, (0.7, 0.8, 1.0))
area("fill", (mx + R, my - R * 0.8, top * 0.4), 6e4 * (span / 60) ** 2, span * 2.0, (1.0, 0.85, 0.95))

# ---- camera: 9:16, looking at the middle of the tower, the whole of it in frame
cam = bpy.data.cameras.new("cam")
cam.lens = 50
cam.sensor_fit = "VERTICAL"
co = bpy.data.objects.new("cam", cam)
sc.collection.objects.link(co)
sc.camera = co
fov = 2 * math.atan(cam.sensor_height / 2 / cam.lens) if False else 2 * math.atan(24 / 2 / cam.lens)
dist = (top * 0.62) / math.tan(fov / 2) + span * 0.5
dist *= float(os.environ.get("TOWER_DIST", "1"))  # >1: further back, more of the scene round it
co.location = (mx - dist * math.cos(EL) * math.sin(AZ), my - dist * math.cos(EL) * math.cos(AZ), top * 0.48 + dist * math.sin(EL))
c = co.constraints.new("TRACK_TO")
c.target = target
if NATURE:
    PULL_MAX = float(os.environ.get("TOWER_PULL", "1.25")) if FUT_L else 1.0
    D_cam = math.hypot(co.location.x - mx, co.location.y - my) * PULL_MAX
    R_new = max(span * float(os.environ.get("TOWER_GRASS_R", "2.4")), D_cam * float(os.environ.get("TOWER_GRASS_REACH", "0.95")))
    GS = float(os.environ.get("TOWER_GRASS_S", "1"))
    circ.inputs["Radius"].default_value = R_new
    gmap.inputs["From Min"].default_value, gmap.inputs["From Max"].default_value = R_new * 0.75, R_new
    dist_node = [nd for nd in ng2.nodes if nd.bl_idname == "GeometryNodeDistributePointsOnFaces"][0]
    dist_node.inputs["Density"].default_value = float(os.environ.get("TOWER_GRASS_D", "3.0")) / GS ** 2
    for r_, lo, hi in ((rh, 0.5, 1.7), (rw, 0.6, 1.2)):
        r_.inputs["Min"].default_value, r_.inputs["Max"].default_value = lo * GS, hi * GS
    print(f"grass: radius {R_new:.0f}, ~{dist_node.inputs['Density'].default_value * math.pi * R_new ** 2 / 1e6:.1f}M blades; camera {D_cam:.0f} away", flush=True)
    fmr.inputs["From Min"].default_value = D_cam * float(os.environ.get("TOWER_HZ_NEAR", "1.0"))
    fmr.inputs["From Max"].default_value = D_cam * float(os.environ.get("TOWER_HZ_FAR", "2.5"))
cam.clip_end = 1e6  # the ground runs out to the horizon
cam.dof.use_dof = True
cam.dof.focus_object = target
cam.dof.aperture_fstop = 8.0

if NATURE and os.environ.get("TOWER_FOG", "0") == "1":
    sc.view_layers[0].use_pass_mist = sc.view_layers[0].use_pass_z = True
    world.mist_settings.start, world.mist_settings.depth = dist * 1.1, dist * float(os.environ.get("TOWER_HAZE_D", "5"))
    world.mist_settings.falloff = "LINEAR"
    sc.use_nodes = True
    ct = sc.node_tree
    for nd in list(ct.nodes):
        ct.nodes.remove(nd)
    rl, comp = ct.nodes.new("CompositorNodeRLayers"), ct.nodes.new("CompositorNodeComposite")
    ground = ct.nodes.new("CompositorNodeMath")
    ground.operation = "LESS_THAN"
    ground.inputs[1].default_value = 1e8  # the sky is at "infinity"
    ct.links.new(rl.outputs["Depth"], ground.inputs[0])
    fac = ct.nodes.new("CompositorNodeMath")
    fac.operation = "MULTIPLY"
    ct.links.new(rl.outputs["Mist"], fac.inputs[0])
    ct.links.new(ground.outputs[0], fac.inputs[1])
    mix = ct.nodes.new("CompositorNodeMixRGB")
    hz = [float(v) for v in os.environ.get("TOWER_HAZE", "0.62,0.42,0.28").split(",")]
    mix.inputs[2].default_value = (*hz, 1.0)
    ct.links.new(fac.outputs[0], mix.inputs["Fac"])
    ct.links.new(rl.outputs["Image"], mix.inputs[1])
    ct.links.new(mix.outputs["Image"], comp.inputs["Image"])
# ---- render: Cycles on the GPU (Metal)
sc.render.engine = "CYCLES"
prefs = bpy.context.preferences.addons["cycles"].preferences
# the GPU: Metal on a Mac (every device, as tested there); NVIDIA elsewhere (OPTIX, or TOWER_DEVICE=CUDA),
# only the cards in TOWER_GPUS ("1": the second one listed) if it's set
prefs.compute_device_type = os.environ.get("TOWER_DEVICE", "METAL" if sys.platform == "darwin" else "OPTIX")
prefs.get_devices()
want = [w for w in os.environ.get("TOWER_GPUS", "").split(",") if w]
gpus = [dev for dev in prefs.devices if dev.type != "CPU"]
for dev in prefs.devices:
    dev.use = sys.platform == "darwin" and not want
for i, dev in enumerate(gpus):
    dev.use = not want or str(i) in want
print("devices:", [(dev.name, dev.type, dev.use) for dev in prefs.devices], flush=True)
if not any(dev.use for dev in gpus):
    raise SystemExit(f"no GPU to render on ({prefs.compute_device_type}); try TOWER_DEVICE=CUDA")
sc.cycles.device = "GPU"
sc.cycles.samples = SAMPLES
sc.cycles.use_denoising = True
sc.cycles.adaptive_threshold = float(os.environ.get("TOWER_NOISE", "0.01"))
sc.render.resolution_x, sc.render.resolution_y = 1080, 1920
sc.view_settings.view_transform = "AgX"
sc.view_settings.look = "AgX - Medium High Contrast"
sc.render.resolution_percentage = PERCENT
sc.view_settings.exposure = float(os.environ.get("TOWER_EXPOSURE", "0"))
sc.render.use_persistent_data = False  # (on, Cycles crashes when the tower clears to rebuild)
sc.render.filepath = OUT
if os.environ.get("TOWER_FORMAT", "PNG").upper() == "JPEG":  # a tenth the size on disk
    sc.render.image_settings.file_format, sc.render.image_settings.quality = "JPEG", 95
if FLY:
    cam.lens = FLY_LENS
    cam.clip_start = 0.05
    cam.dof.use_dof = False
    co.constraints.clear()
    co.rotation_mode = "XYZ"
    lamp = bpy.data.lights.new("headlamp", "POINT")  # it lights the undersides it flies toward; far ones fall dark
    lamp.energy, lamp.shadow_soft_size, lamp.color = float(os.environ.get("TOWER_LAMP", "4000")), 1.5, (1.0, 0.95, 0.9)
    lo = bpy.data.objects.new("headlamp", lamp)
    sc.collection.objects.link(lo)
    lo.parent = co
    lo.location = (0, 0, 0)
    sc.render.use_motion_blur = os.environ.get("TOWER_BLUR", "1") == "1"
    sc.render.use_persistent_data = True  # nothing changes but the camera (the crash was a tower clearing)
    sc.render.motion_blur_shutter = 0.5
    GX = max(2, int(FLY_N * FLY_EXIT))
    from mathutils import Euler, Vector
    out_z = top + float(os.environ.get("TOWER_FLY_OUT", "0.9")) * span  # where it ends up: high above today, looking down
    end_at = Vector((mx, my, top))

    def inside(f):
        k = (f - 1) / (GX - 1)
        e = k * k * (3 - 2 * k) * 0.25 + k * 0.75  # a little ease at the ends
        return Vector((FX, FY, 0.6 + e * (top + 1.5 - 0.6))), Euler((math.pi - FLY_TILT, 0.0, FLY_YAW * e))

    T0, T1 = GX - 45, GX + 20  # the turn: it starts inside (strands seen side on), so it comes out already looking down
    from mathutils import Matrix
    # the end: straight over today's board, turned a quarter so its 16:9 fills the 9:16 frame, tipped a little
    END_TILT = math.radians(float(os.environ.get("TOWER_END_TILT", "16")))
    end_d = 1.08 * (W / 2) / math.tan(math.atan(12 / FLY_LENS))  # the board's length (W) fills the frame's height
    fwd = Vector((math.sin(END_TILT), 0.0, -math.cos(END_TILT)))  # down, leaning toward +x (the frame's top)
    p1 = end_at - fwd * end_d
    up = Vector((1.0, 0.0, 0.0))
    up = (up - fwd * up.dot(fwd)).normalized()
    right = fwd.cross(up).normalized()
    look = Matrix((right, up, -fwd)).transposed().to_quaternion()

    hero_at = Vector((mx, my, top * 0.5))  # the swoop's end: the whole tower, side on, as the noon tower opens
    hd = float(os.environ.get("TOWER_HERO_D", "1.2")) * max(top * 0.5 / math.tan(math.atan(12 / FLY_LENS)),
                                                        span * 0.6 / math.tan(math.atan(12 * 9 / 16 / FLY_LENS)))
    hero_p = hero_at + Vector((-math.sin(AZ) * math.cos(EL), -math.cos(AZ) * math.cos(EL), math.sin(EL))) * hd
    hero_q = (hero_at - hero_p).to_track_quat("-Z", "Y")
    SWOOP = float(os.environ.get("TOWER_SWOOP", "0.62"))  # of the frames after the flight: moving; then it holds

    def pose(f):  # (location, rotation) at frame f
        if f > FLY_N:  # from over today's board, back and down to the whole tower
            p_end, r_end = pose(FLY_N)
            u = min(1.0, (f - FLY_N) / max(1, (FRAMES - FLY_N) * SWOOP))
            e = u * u * (3 - 2 * u)
            return p_end.lerp(hero_p, e), r_end.to_quaternion().slerp(hero_q, e).to_euler("XYZ")
        p0, r0 = inside(min(f, GX))
        if f < T0:
            return p0, r0
        if f > GX:
            k = (f - GX) / max(1, FLY_N - GX)
            rise = min(1.0, k * 1.25) ** 2.2  # just above the top (today's skyline), then up
            p0 = p0.lerp(p1, rise)
        u = min(1.0, (f - T0) / (T1 - T0))
        q = inside(T0)[1].to_quaternion().slerp(look, u * u * (3 - 2 * u))
        return p0, q.to_euler("XYZ")
    def set_grow(v, f=None):  # the growth front (flying with AHEAD: just above the camera)
        for gm in grow_mods:
            gm[key] = v
            if f is not None:
                gm.keyframe_insert(data_path=f'["{key}"]', frame=f)

    def grow_at(f):  # inside: AHEAD above the camera; out of the top: the whole of it
        if f > GX:
            return full
        return float(min(full, inside(f)[0].z + AHEAD))
    if AHEAD:
        sc.render.use_persistent_data = False  # the geometry changes every frame now
    DIVE = int(os.environ.get("TOWER_DIVE", "0"))  # >0: render only the opening: from the whole tower into its base
    if DIVE:
        p_in, r_in = inside(1)
        q_in = r_in.to_quaternion()
        out = Vector((hero_p.x - p_in.x, hero_p.y - p_in.y, 0)).normalized()
        ctrl = p_in + out * span * 0.45 + Vector((0, 0, top * 0.05))  # in low, from the hero's side, under the overhang

        def dive(f):
            u = (f - 1) / max(1, DIVE - 1)
            t = u * u * (3 - 2 * u)
            pos = hero_p * (1 - t) ** 2 + ctrl * (2 * (1 - t) * t) + p_in * t * t
            return pos, hero_q.slerp(q_in, t ** 1.6).to_euler("XYZ")
        for f in range(1, DIVE + 1):
            co.location, co.rotation_euler = dive(f)
            co.keyframe_insert("location", frame=f)
            co.keyframe_insert("rotation_euler", frame=f)
            if AHEAD:  # it un-builds from the top down as the camera dives in (and rebuilds as it flies up)
                u = (f - 1) / max(1, DIVE - 1)
                t = u * u * (3 - 2 * u)
                set_grow(full + (grow_at(1) - full) * t, f)
        if FLY_TEST:
            for frac in [float(v) for v in FLY_TEST.split(",")]:
                sc.frame_set(max(1, int(round(frac * DIVE))))
                sc.render.filepath = f"{OUT}_{int(frac * 100):02d}.png"
                bpy.ops.render.render(write_still=True)
            sys.exit(0)
        sc.frame_start, sc.frame_end = START_FRAME, DIVE
        sc.render.fps = 30
        bpy.ops.render.render(animation=True)
        print("wrote", OUT, flush=True)
        sys.exit(0)
    if FLY_TEST:
        for frac in [float(v) for v in FLY_TEST.split(",")]:
            f = max(1, int(round(frac * FRAMES)))
            co.location, co.rotation_euler = pose(f)
            if AHEAD:
                set_grow(grow_at(min(f, FLY_N)))
            sc.render.filepath = f"{OUT}_{int(frac * 100):02d}.png"
            bpy.ops.render.render(write_still=True)
        sys.exit(0)
    for f in range(1, FRAMES + 1):
        co.location, co.rotation_euler = pose(f)
        co.keyframe_insert("location", frame=f)
        co.keyframe_insert("rotation_euler", frame=f)
        if AHEAD:
            set_grow(grow_at(min(f, FLY_N)), f)
    sc.frame_start, sc.frame_end = START_FRAME, FRAMES
    sc.frame_step = int(os.environ.get("TOWER_STEP", "1"))
    sc.render.fps = 30
    bpy.ops.render.render(animation=True)
elif FRAMES:  # the camera circles the tower's middle
    pivot = bpy.data.objects.new("pivot", None)
    pivot.location = target.location
    sc.collection.objects.link(pivot)
    co.parent = pivot
    co.location = tuple(co.location[i] - pivot.location[i] for i in range(3))
    pivot.rotation_euler = (0, 0, 0)
    pivot.keyframe_insert("rotation_euler", frame=1)
    if FUT_L:
        PULL, LIFT = float(os.environ.get("TOWER_PULL", "1.25")), float(os.environ.get("TOWER_LIFT", "0.12"))
        OFFZ = co.location.z  # the camera's height off the pivot, at scale 1 (below it: it looks up a little)
        CAM_MIN = float(os.environ.get("TOWER_CAM_MIN", "5.5"))  # above the grass
        Zs = lambda k_: top * (0.48 + LIFT) * k_
        k_now = NOW_H / full
        if SPROUT:  # close on the seed in the grass, looking down at it; rising and pulling back as it grows
            S0, S1 = (float(v) for v in os.environ.get("TOWER_SEED_S", "0.04,0.14").split(","))
            SEED_Z = float(os.environ.get("TOWER_SEED_Z", "1.2"))
            cam_keys = [(1, k_now, Zs(k_now), Zs(k_now), 1.0), (HOLD0, k_now, Zs(k_now), Zs(k_now), 1.0),
                        (G0, S0, max(Zs(S0), CAM_MIN - S0 * OFFZ), SEED_Z, 0.0),
                        (G0 + SPROUT, S1, max(Zs(S1), CAM_MIN - S1 * OFFZ), Zs(S1) * 0.8, 0.4),
                        (G1, k_now, Zs(k_now), Zs(k_now), 1.0), (GF, PULL, Zs(1.0), Zs(1.0), 1.0), (FRAMES, PULL, Zs(1.0), Zs(1.0), 1.0)]
        else:
            cam_keys = [(1, k_now, Zs(k_now), Zs(k_now), 1.0), (G0, k_now, Zs(k_now), Zs(k_now), 1.0),
                        (GF, PULL, Zs(1.0), Zs(1.0), 1.0), (FRAMES, PULL, Zs(1.0), Zs(1.0), 1.0)]
        for f, sc_, pz, tz, c_ in cam_keys:  # c_: 1 centred on the tree, 0 on its seed
            pivot.scale = (sc_, sc_, sc_)
            pivot.keyframe_insert("scale", frame=f)
            for o, z in ((pivot, pz), (target, tz)):
                o.location = (mx * c_, my * c_, z)
                o.keyframe_insert("location", frame=f)
            print(f"camera key {f}: scale {sc_:.3f}, camera at z {pz + sc_ * OFFZ:.1f}, looking at z {tz:.1f}", flush=True)

        def f_at(layer_i):  # the frame the growth reaches this layer (in the future)
            return G1 + (layer_i * LAYER - NOW_H) / (full - NOW_H) * (GF - G1)
        f_fz, f_inj = f_at(FROZEN_L), f_at(INJ_L)
        print(f"frozen at frame {f_fz:.0f}, the comment at {f_inj:.0f}", flush=True)
        DMAX, DIM = float(os.environ.get("TOWER_DEAD_MAX", "0.85")), float(os.environ.get("TOWER_DIM", "0.45"))
        BACK = int(os.environ.get("TOWER_BACK", "45"))  # frames for the comment to bring it all back
        DEATH_END = f_inj - int(os.environ.get("TOWER_DEAD_BY", "12"))  # dead to the ground a little before the comment
        for df, lf in FRONTS:
            for f, z in ((1, NOW_H + SOFT), (G1, NOW_H + SOFT), (DEATH_END, -SOFT)):
                df.outputs[0].default_value = z
                df.outputs[0].keyframe_insert("default_value", frame=int(round(f)))
            for f, z in ((1, NOW_H + 2 * SOFT), (f_inj, NOW_H + 2 * SOFT), (f_inj + BACK, -2 * SOFT)):
                lf.outputs[0].default_value = z
                lf.outputs[0].keyframe_insert("default_value", frame=int(round(f)))
            for v_ in (df, lf):
                for fc in v_.id_data.animation_data.action.fcurves:
                    for kp in fc.keyframe_points:
                        kp.interpolation = "LINEAR"
        if NATURE:  # the light goes as it dies, and comes back with the comment
            E_sun, E_sky = sl.energy, bg.inputs["Strength"].default_value
            for f, v in ((1, 1.0), (G1, 1.0), (DEATH_END, 1.0 - DIM), (f_inj, 1.0 - DIM), (f_inj + BACK, 1.0)):
                sl.energy = E_sun * v
                sl.keyframe_insert("energy", frame=int(round(f)))
                for sock in (bg.inputs["Strength"], fem.inputs["Strength"]):
                    sock.default_value = E_sky * v
                    sock.keyframe_insert("default_value", frame=int(round(f)))
        if INJ_L is not None:  # the comment lands: a flash of warm light where it does
            sel = mt_ == INJ_L
            fx, fy, fz_ = float(mx_[sel].mean()) + 0.5, -float(my_[sel].mean()) - 0.5, INJ_L * LAYER + 2.0
            fl = bpy.data.lights.new("flash", "POINT")
            fl.shadow_soft_size, fl.color = 3.0, (1.0, 0.88, 0.7)
            flo = bpy.data.objects.new("flash", fl)
            flo.location = (fx, fy, fz_)
            sc.collection.objects.link(flo)
            E = float(os.environ.get("TOWER_FLASH", "30000"))
            for f, e in ((1, 0.0), (f_inj - 1, 0.0), (f_inj + 2, E), (f_inj + 12, E * 0.5), (f_inj + 45, 0.0)):
                fl.energy = e
                fl.keyframe_insert("energy", frame=int(round(f)))
    else:
        mid = int(G0 + DIVE_AT * (GF - G0))
        fz = (top * 0.48, top * 0.48, top * DIVE_Z[0], top * DIVE_Z[1], top * 0.48)
        for o in (pivot, target):  # the whole tower; then close on where it began, rising and pulling back as it grows
            for f, z in zip((1, HOLD0, G0, mid, GF), fz):
                o.location.z = z
                o.keyframe_insert("location", index=2, frame=f)
            for f, (px, py) in zip((1, HOLD0, G0, mid, GF), ((mx, my), (mx, my), (0.0, 0.0), (0.0, 0.0), (mx, my))):
                o.location.x, o.location.y = px, py
                o.keyframe_insert("location", index=0, frame=f)
                o.keyframe_insert("location", index=1, frame=f)
        for f, k in zip((1, HOLD0, G0, mid, GF), (1.0, 1.0, DIVE_SCALE[0], DIVE_SCALE[1], 1.0)):
            pivot.scale = (k, k, k)
            pivot.keyframe_insert("scale", frame=f)
    pivot.rotation_euler = (0, 0, math.radians(ORBIT) * FRAMES / (FRAMES + 1))  # the frame after the last is the first
    pivot.keyframe_insert("rotation_euler", frame=FRAMES)
    for fc in pivot.animation_data.action.fcurves:
        if fc.data_path == "rotation_euler":
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
    if NATURE:
        so.parent = pivot
        a_end = math.radians(ORBIT) * FRAMES / (FRAMES + 1)
        for nd_ in (sky, fsky):
            base = nd_.sun_rotation
            for f, a_ in ((1, 0.0), (FRAMES, a_end)):
                nd_.sun_rotation = base - a_
                nd_.keyframe_insert("sun_rotation", frame=f)
            nd_.sun_rotation = base
        for tree_ in (world.node_tree, fm.node_tree):
            for fc in tree_.animation_data.action.fcurves:
                if fc.data_path.endswith("sun_rotation"):
                    for kp in fc.keyframe_points:
                        kp.interpolation = "LINEAR"
    sc.frame_start, sc.frame_end = START_FRAME, FRAMES
    sc.frame_step = int(os.environ.get("TOWER_STEP", "1"))
    if FUT_L:
        sc.render.use_persistent_data = True
    sc.render.fps = 30
    bpy.ops.render.render(animation=True)
else:
    bpy.ops.render.render(write_still=True)
print("wrote", OUT, flush=True)
