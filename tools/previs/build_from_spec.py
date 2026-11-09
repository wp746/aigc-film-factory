"""在 Blender 里按镜头规格 JSON 搭白模预演并渲染帧序列。由 render_previs.py 调用：

    <Blender> -b --factory-startup -P build_from_spec.py -- <spec.json> <帧输出目录> [--stills]

坐标：米制，Z 向上。人物 face=0 时正面朝 -Y，face 为绕 Z 轴的角度（度，逆时针为正）。
规格字段见 tools/previs/README.md。
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *  # noqa: E402,F401  (bpy, reset, box, person, key, camera, linear_all)

argv = sys.argv[sys.argv.index("--") + 1:]
SPEC = json.load(open(argv[0], encoding="utf-8"))
OUT = argv[1]
STILLS = "--stills" in argv
NO_RENDER = "--no-render" in argv  # 只搭场景不渲染（geometry_check.py 用）

dur = float(SPEC["duration"])
sc = reset(dur)
res = SPEC.get("resolution", [1280, 720])
sc.render.resolution_x, sc.render.resolution_y = res
if "world_color" in SPEC:
    sc.world.color = SPEC["world_color"]


def keyed_times(keys):
    """按时间排序；同一时刻写了两个位置（切点上换了地方）时，前一个提前一帧。
    Blender 一帧只留一个关键帧，不错开的话人会从前一个地方慢慢滑到后一个地方（《好日子》第 8 条：秀英从镜头前穿过去）。"""
    ks = sorted(keys, key=lambda k: k["t"])
    out = []
    for i, k in enumerate(ks):
        if i + 1 < len(ks) and abs(ks[i + 1]["t"] - k["t"]) < 1e-6:
            k = dict(k, t=max(0.0, k["t"] - 1.0 / FPS))
        out.append(k)
    return out


# 静态与可动方块：min/max 是初始包围盒；keys 里的 offset 是相对初始位置的位移，scale 是缩放倍数
for b in SPEC.get("boxes", []):
    o = box(b["name"], b["min"][0], b["max"][0], b["min"][1], b["max"][1], b["min"][2], b["max"][2],
            tuple(b.get("color", (0.4, 0.4, 0.4))))
    base_loc, base_scale = tuple(o.location), tuple(o.scale)
    for k in keyed_times(b.get("keys", [])):
        off = k.get("offset", (0, 0, 0))
        sc_mul = k.get("scale", (1, 1, 1))
        key(o, k["t"], loc=tuple(base_loc[i] + off[i] for i in range(3)),
            scale=tuple(base_scale[i] * sc_mul[i] for i in range(3)))

# 人物：4.0 alpha.8 起默认用关节小人（rig），按 poses 做姿态，走动时自动摆臂摆腿；"rig": false 退回圆柱 + 球头
aims = {}
rides = []
for p in SPEC.get("people", []):
    h = p.get("height", 1.70)
    if p.get("shape") == "giant":  # 巨物形状代理：树干身子、根须、枝、空心环头（不做关节和姿态）
        parts, root = None, giant(p["name"], tuple(p.get("color", (0.6, 0.35, 0.8))), h)
    else:
        use_rig = p.get("rig", SPEC.get("rig", True))
        parts = rig(p["name"], tuple(p.get("color", (0.85, 0.25, 0.10))), h) if use_rig else None
        root = parts["root"] if parts else person(p["name"], tuple(p.get("color", (0.85, 0.25, 0.10))), h)
    aim = bpy.data.objects.new(p["name"] + "_aim", None)
    bpy.context.collection.objects.link(aim)
    aims[p["name"]] = aim
    ks = keyed_times(p["keys"])
    for k in ks:
        x, y, z = k["pos"]
        key(root, k["t"], loc=(x, y, z), rot=(-k.get("lean", 0), 0, k.get("face", 0)))
        key(aim, k["t"], loc=(x, y, z + 0.8 * h))
    if p.get("ride"):  # 坐在车里：人和他的注视点用约束挂在车上，keys 里的位置就是座位偏移
        rides.append((p, root, aim))
    if p.get("shape") == "giant" and any(k.get("head") for k in ks):
        giant_head(p["name"], tuple(p.get("color", (0.6, 0.35, 0.8))), h, ks)
    if parts:
        moving = []
        for a, b in zip(ks, ks[1:]):
            dt = b["t"] - a["t"]
            dist = math.dist(a["pos"][:2], b["pos"][:2])
            moving.append((a["t"], dist / dt if dt > 1e-6 else 0.0))
        moving.append((ks[-1]["t"], 0.0))
        p["_parts"], p["_moving"] = parts, moving
# 道具先建（跟手、放下、摆动、出现消失），姿态最后加：伸手的目标可能是道具，也可能是别人的手
for pr in SPEC.get("props", []):
    aims["prop_" + pr["name"]] = prop_object(pr)  # 镜头可以盯住移动的道具（look_at: "prop_花轿"）
for fx in SPEC.get("effects", []):  # 特效代理：碎石、裂缝、落屑、巨物的臂
    effect(fx)
for p, root, aim in rides:
    car = aims.get(p["ride"])
    if car is None:
        continue
    for o, dz in ((root, 0.0), (aim, 0.8 * p.get("height", 1.70))):
        cl = o.constraints.new("COPY_LOCATION")
        cl.target, cl.use_offset = car, True
        sx, sy, sz = p.get("seat", (-0.4, 0.2, 0.1))
        hh = car.dimensions.z / 2  # 道具的原点在车身中心，座位偏移从车底板算
        for fc_t in sorted({k["t"] for k in p["keys"]}):
            key(o, fc_t, loc=(sx, sy, sz - hh + dz))
for p in SPEC.get("people", []):
    if p.get("_parts"):
        apply_poses(p["_parts"], p["name"], p.get("height", 1.70), p.get("poses", []), dur, p["_moving"])


def wobble(t, amp):
    return (amp * math.sin(t * 7.1), amp * math.sin(t * 5.3 + 1), amp * math.sin(t * 6.2 + 2))


# 镜头：每个 shot 从 start 开始，到下一个 shot 的 start 结束；时间轴标记绑定相机实现硬切
shots = sorted(SPEC["shots"], key=lambda s: s["start"])
cams, eased, mounted = [], [], []  # eased：要缓入缓出的运镜机位（规格 ease: true）；mounted：挂在人、车上的机位
for i, s in enumerate(shots):
    c = camera(s.get("lens", 35))
    c.name = s.get("name", f"C{i + 1}")
    end = shots[i + 1]["start"] if i + 1 < len(shots) else dur
    target = s.get("look_at")
    if target is not None:
        aim = aims.get(target) if isinstance(target, str) else None
        if aim is not None and s.get("look_off"):  # 盯住移动道具上偏开一点的位置（轿子前沿），构图不是一整面墙
            off = bpy.data.objects.new(c.name + "_target", None)
            bpy.context.collection.objects.link(off)
            off.location = tuple(s["look_off"])
            cl = off.constraints.new("COPY_LOCATION")
            cl.target = aim
            cl.use_offset = True
            aim = off
        if aim is None:
            aim = bpy.data.objects.new(c.name + "_target", None)
            bpy.context.collection.objects.link(aim)
            aim.location = target
        con = c.constraints.new("TRACK_TO")
        con.target = aim
        con.track_axis = "TRACK_NEGATIVE_Z"
        con.up_axis = "UP_Y"
    if s.get("mount"):
        mounted.append((s, c, s["start"], end))
        cams.append((s["start"], c))
        m = sc.timeline_markers.new(c.name, frame=int(1 + s["start"] * FPS))
        m.camera = c
        continue
    ks = keyed_times(s["keys"])
    amp = s.get("handheld", 0.0)
    times = sorted({k["t"] for k in ks} | ({round(s["start"] + j * 0.25, 2) for j in range(int((end - s["start"]) / 0.25) + 1)} if amp else set()))
    for t in times:
        # 在相邻关键帧之间线性插值，再叠加手持晃动
        prev = max((k for k in ks if k["t"] <= t), key=lambda k: k["t"], default=ks[0])
        nxt = min((k for k in ks if k["t"] >= t), key=lambda k: k["t"], default=ks[-1])
        u = 0 if nxt["t"] == prev["t"] else (t - prev["t"]) / (nxt["t"] - prev["t"])
        if s.get("ease") and amp:  # 手持叠在缓动的运镜上：逐点采样时自己缓
            u = u * u * (3 - 2 * u)
        pos = [prev["pos"][j] + (nxt["pos"][j] - prev["pos"][j]) * u for j in range(3)]
        w = wobble(t, amp)
        pos = tuple(pos[j] + w[j] for j in range(3))
        rot = None
        if target is None:
            r0, r1 = prev.get("rot", (90, 0, 0)), nxt.get("rot", (90, 0, 0))
            rot = tuple(r0[j] + (r1[j] - r0[j]) * u for j in range(3))
        key(c, t, loc=pos, rot=rot if rot else (90, 0, 0))
    if ks[-1]["t"] < end:
        key(c, end, loc=tuple(ks[-1]["pos"]), rot=ks[-1].get("rot", (90, 0, 0)) if target is None else (90, 0, 0))
    cams.append((s["start"], c))
    if s.get("ease") and not amp and len(ks) > 1:
        eased.append(c)
    m = sc.timeline_markers.new(c.name, frame=int(1 + s["start"] * FPS))
    m.camera = c
sc.camera = cams[0][1]

linear_all()
ease_all(eased)
for s, c, t0, t1 in mounted:  # 挂载机位最后搭：要用到人和道具的约束
    mount_camera(s, c, t0, t1, aims)
flush_hidden()
if SPEC.get("light"):  # 光型（正午顶光、落日斜光……）：换 EEVEE + 太阳灯；没写 light 保持 Workbench 平光
    apply_light(SPEC["light"])
os.makedirs(OUT, exist_ok=True)
if NO_RENDER:
    pass
elif STILLS:
    # 每个镜头取中点一帧
    for i, (t0, c) in enumerate(cams):
        t1 = cams[i + 1][0] if i + 1 < len(cams) else dur
        f = int(1 + (t0 + t1) / 2 * FPS)
        sc.camera = c
        sc.frame_set(f)
        sc.render.filepath = f"{OUT}/still_{i + 1:02d}.png"
        bpy.ops.render.render(write_still=True)
else:
    render(OUT)
