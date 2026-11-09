import bpy, math

FPS = 24


def reset(seconds):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.fps = FPS
    sc.frame_start = 1
    sc.frame_end = int(seconds * FPS)
    sc.render.resolution_x, sc.render.resolution_y = 1280, 720
    sc.render.resolution_percentage = 100
    try:
        sc.render.engine = 'BLENDER_WORKBENCH'
    except TypeError as e:
        print(e)
    sh = sc.display.shading
    sh.light = 'STUDIO'
    sh.color_type = 'OBJECT'
    sh.show_shadows = True
    sh.show_cavity = False
    w = bpy.data.worlds.new("W")
    sc.world = w
    w.color = (0.55, 0.58, 0.62)
    sc.render.film_transparent = False
    sc.render.image_settings.file_format = 'PNG'
    return sc


def box(name, x0, x1, y0, y1, z0, z1, color):
    bpy.ops.mesh.primitive_cube_add(size=1)
    o = bpy.context.object
    o.name = name
    o.scale = (x1 - x0, y1 - y0, z1 - z0)
    o.location = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    o.color = (*color, 1)
    return o


def cyl(name, r, h, loc, color, verts=24):
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=h, vertices=verts, location=loc)
    o = bpy.context.object
    o.name = name
    o.color = (*color, 1)
    return o


def person(name, color, height=1.65):
    """Cylinder + sphere head proxy; front (dark patch) faces -Y at rotation 0."""
    root = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(root)
    hd = 0.2 * height
    body_h = height - hd
    b = cyl(name + "_body", 0.12 * height, body_h, (0, 0, body_h / 2), color)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=hd / 2, location=(0, 0, body_h + hd / 2))
    h = bpy.context.object
    h.name = name + "_head"
    h.color = (*color, 1)
    m = box(name + "_front", -0.08, 0.08, -0.12 * height - 0.03, -0.12 * height + 0.02,
            body_h * 0.55, body_h * 0.85, (0.05, 0.05, 0.05))
    nose = box(name + "_face", -0.04, 0.04, -hd / 2 - 0.03, -hd / 2 + 0.03,
               body_h + hd * 0.4, body_h + hd * 0.6, (0.05, 0.05, 0.05))
    for o in (b, h, m, nose):
        o.parent = root
    return root


def key(obj, t, loc=None, rot=None, scale=None):
    f = 1 + t * FPS
    if loc is not None:
        obj.location = loc
        obj.keyframe_insert("location", frame=f)
    if rot is not None:
        obj.rotation_euler = [math.radians(a) for a in rot]
        obj.keyframe_insert("rotation_euler", frame=f)
    if scale is not None:
        obj.scale = scale
        obj.keyframe_insert("scale", frame=f)


def camera(lens_mm):
    cd = bpy.data.cameras.new("Cam")
    cd.lens = lens_mm
    cd.sensor_width = 36
    cd.clip_end = 5000
    c = bpy.data.objects.new("Cam", cd)
    bpy.context.collection.objects.link(c)
    bpy.context.scene.camera = c
    return c


def linear_all():
    for o in bpy.data.objects:
        ad = o.animation_data
        if not ad or not ad.action:
            continue
        try:
            curves = ad.action.fcurves
        except AttributeError:
            curves = [fc for layer in ad.action.layers for strip in layer.strips
                      for bag in strip.channelbags for fc in bag.fcurves]
        for fc in curves:
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'


def ease_all(objs, paths=("location",)):
    """运镜缓入缓出：这些物体的关键帧改成两端平的贝塞尔（自动钳制手柄），起步慢、中间快、收尾慢，不会冲过头。
    在 linear_all() 之后调用。"""
    for o in objs:
        ad = o.animation_data
        if not ad or not ad.action:
            continue
        try:
            curves = ad.action.fcurves
        except AttributeError:
            curves = [fc for layer in ad.action.layers for strip in layer.strips
                      for bag in strip.channelbags for fc in bag.fcurves]
        for fc in curves:
            if not fc.data_path.startswith(paths):
                continue
            for kp in fc.keyframe_points:
                kp.interpolation = 'BEZIER'
                kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
            fc.update()


def render(out_dir, frames=None):
    sc = bpy.context.scene
    if frames:
        for f in frames:
            sc.frame_set(f)
            sc.render.filepath = f"{out_dir}/still_{f:04d}.png"
            bpy.ops.render.render(write_still=True)
    else:
        sc.render.filepath = f"{out_dir}/f_"
        bpy.ops.render.render(animation=True)


# ---------------- 4.0 alpha.8：关节小人和姿态（精准白模第一期） ----------------
# 小人正面朝 -Y（和 person() 一样）。关节都是空物体，旋转 X 轴：手臂、腿向前抬是负角度，躯干向前弯是正角度。
# 名字沿用 <名>_body / <名>_head，几何检查不用改；另有 <名>_handL / _handR（手的位置，道具挂在这里、伸手的目标也看这里）。

def _empty(name, parent=None, loc=(0, 0, 0)):
    e = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(e)
    if parent is not None:
        e.parent = parent
    e.location = loc
    e.rotation_mode = 'XYZ'
    return e


def rig(name, color, height=1.65, skin=(0.86, 0.68, 0.56)):
    h = height
    root = _empty(name)
    hips = _empty(name + "_hips", root, (0, 0, 0.52 * h))
    parts = {"root": root, "hips": hips}
    for side, sx in (("L", 1), ("R", -1)):  # 正面朝 -Y 时，人的左手在 +X
        hp = _empty(f"{name}_hip{side}", hips, (sx * 0.06 * h, 0, 0))
        leg = box(f"{name}_leg{side}", -0.035 * h, 0.035 * h, -0.04 * h, 0.04 * h, -0.52 * h, 0, tuple(c * 0.8 for c in color))
        leg.parent = hp
        parts["hip" + side] = hp
    spine = _empty(name + "_spine", hips)
    parts["spine"] = spine
    b = cyl(name + "_body", 0.11 * h, 0.32 * h, (0, 0, 0.16 * h), color)
    b.parent = spine
    m = box(name + "_front", -0.07 * h, 0.07 * h, -0.115 * h, -0.10 * h, 0.12 * h, 0.26 * h, (0.05, 0.05, 0.05))
    m.parent = spine
    neck = _empty(name + "_neck", spine, (0, 0, 0.30 * h))
    parts["neck"] = neck
    # 头按真人比例（直径约身高的 15%）：原来 20% 的大头在特写里会塞满画面，构图比成片紧（《好日子》第 6 条镜 6）
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.075 * h, location=(0, 0, 0))
    hd = bpy.context.object
    hd.name = name + "_head"
    hd.color = (*color, 1)
    hd.parent = neck
    hd.location = (0, 0, 0.085 * h)
    nose = box(name + "_face", -0.025 * h, 0.025 * h, -0.085 * h, -0.06 * h, 0.07 * h, 0.105 * h, (0.05, 0.05, 0.05))
    nose.parent = neck
    for side, sx in (("L", 1), ("R", -1)):
        sh = _empty(f"{name}_sh{side}", spine, (sx * 0.15 * h, 0, 0.28 * h))
        arm = box(f"{name}_arm{side}", -0.025 * h, 0.025 * h, -0.025 * h, 0.025 * h, -0.34 * h, 0, color)
        arm.parent = sh
        hand = box(f"{name}_hand{side}_mesh", -0.03 * h, 0.03 * h, -0.02 * h, 0.02 * h, -0.42 * h, -0.34 * h, skin)
        hand.parent = sh
        he = _empty(f"{name}_hand{side}", sh, (0, 0, -0.38 * h))
        parts["sh" + side], parts["hand" + side] = sh, he
    return parts


# 姿态表：hips 是髋部下沉（占身高的比例），其余是角度（度）。没写的关节回到 0。
POSES = {
    "stand": {}, "walk": {}, "run": {"spine": 12},
    "sit": {"hips": -0.24, "hipL": -90, "hipR": -90},
    "sit_ground": {"hips": -0.46, "hipL": -90, "hipR": -90, "spine": 5},
    "kneel": {"hips": -0.25, "hipL": 90, "hipR": 90},
    "kowtow": {"hips": -0.25, "hipL": 90, "hipR": 90, "spine": 80, "shL": -75, "shR": -75},
    "bend": {"spine": 70, "shL": -30, "shR": -30},
    "crouch": {"hips": -0.2, "hipL": -60, "hipR": -60, "spine": 30},
    "pick": {"hips": -0.3, "hipL": -70, "hipR": -70, "spine": 55},   # 蹲下去捡地上的东西：手能碰到地面
    "raise_arms": {"shL": -170, "shR": -170},
    "carry": {"shL": -150, "shR": -150},          # 抬轿：手扶肩上的杠
    "hands_front": {"shL": -60, "shR": -60},       # 端着、捧着
    "lean_back": {"spine": -20, "shL": -70, "shR": -70},
    "dragged": {"spine": -15, "shL": -100, "shR": -100},
    "fall": {"spine": 40},
    "nod": {}, "comb": {}, "pull": {}, "struggle": {}, "laugh": {"spine": -8},
    "reach": {}, "grab": {}, "give": {}, "point": {}, "hold": {}, "look": {},
    # 打戏（4.0 alpha.12，高难镜头压力测试）：出拳 / 刺拳的手臂伸向 target，过渡极短；没有肘关节，用肩和躯干表现
    "punch": {"spine": 12}, "jab": {"spine": 5},
    "kick": {"hipR": -85, "spine": -18, "shL": -40, "shR": 25},           # 正蹬：右腿抬平，上身后仰保持平衡
    "block": {"shL": -125, "shR": -125, "spine": 8, "neck": 15},          # 双臂护在脸前，缩头
    "duck": {"hips": -0.3, "hipL": -75, "hipR": -75, "spine": 45},        # 矮身闪躲：比 crouch 更低、更前倾
    "stagger": {"spine": -28, "shL": -50, "shR": -35, "neck": -20},       # 挨打后仰踉跄
    "lie": {"hips": -0.47, "hipL": -90, "hipR": -90, "spine": -90, "neck": -10, "shL": -170, "shR": -170},  # 仰面躺在地上：腿朝面前、上身朝背后放平
    "slump": {"hips": -0.43, "hipL": -80, "hipR": -80, "spine": -25, "neck": 20, "shL": -15, "shR": -15},  # 顺着墙滑坐到地上，头垂下
}
FAST = {"punch": 0.1, "jab": 0.08, "kick": 0.15, "block": 0.12, "duck": 0.15, "stagger": 0.12}  # 打戏姿态的过渡时间（秒）
OSC = {  # 反复的动作：(关节, 振幅, 每秒次数)
    "nod": [("neck", 18, 1.6)], "comb": [("shR", 22, 1.2)], "pull": [("shL", 60, 0.8), ("shR", 60, 0.8)],
    "struggle": [("spine", 12, 2.0), ("shL", 30, 2.2), ("shR", 30, 1.8)], "laugh": [("spine", 5, 3.0)],
}
REACH = {"reach", "grab", "give", "point", "hold", "comb", "pull", "punch", "jab"}  # 手伸向 target
RAMP = 0.4


def _target_obj(target, objs, name):
    """target：'人名:handR' / '人名:head' / '人名' / 道具名 / [x,y,z]。"""
    if isinstance(target, (list, tuple)):
        e = _empty(f"{name}_tgt_{len(bpy.data.objects)}")
        e.location = target
        return e
    if isinstance(target, str):
        if ":" in target:
            who, part = target.split(":", 1)
            part = {"手": "handR", "左手": "handL", "右手": "handR", "头": "head"}.get(part, part)
            o = bpy.data.objects.get(f"{who}_{part}")
            if o:
                return o
        return bpy.data.objects.get(target) or bpy.data.objects.get(target + "_head") or objs.get(target)
    return None


def apply_poses(parts, name, h, poses, dur, moving=None):
    """poses：[{t0, t1, pose, target, arm}]；moving：[(t, 速度 米/秒)] 用来生成走路摆臂摆腿。"""
    poses = sorted(poses or [], key=lambda p: p["t0"])
    joints = ("hips", "spine", "neck", "shL", "shR", "hipL", "hipR")

    def ramp(p):
        """过渡时间：打戏姿态用 FAST；其余默认 0.4 秒，但不超过这段节拍的 45%（0.35 秒的一拳要伸得直）。"""
        if "ramp" in p:
            return p["ramp"]
        return FAST.get(p["pose"], min(RAMP, max(0.08, (p["t1"] - p["t0"]) * 0.45)))

    def weight(p, t):
        r = ramp(p)
        if t < p["t0"] or t > p["t1"] + r:
            return 0.0
        if t < p["t0"] + r:
            return (t - p["t0"]) / r
        return 1.0 if t <= p["t1"] else max(0.0, 1 - (t - p["t1"]) / r)

    def active(t):  # 身体姿态（坐、跪、弯腰……）和手势（点头、伸手……）叠加：每个关节取各段的加权和
        return [p for p in poses if weight(p, t) > 0]

    def value(j, t):
        v = 0.0
        for p in active(t):
            w = weight(p, t)
            v += POSES.get(p["pose"], {}).get(j, 0.0) * w
            for jj, amp, hz in OSC.get(p["pose"], []):
                if jj == j and p["t0"] + ramp(p) <= t <= p["t1"]:
                    v += amp * math.sin(2 * math.pi * hz * (t - p["t0"]))
        return v

    def speed(t):
        if not moving:
            return 0.0
        prev = [m for m in moving if m[0] <= t]
        return prev[-1][1] if prev else 0.0

    times = {0.0, round(dur, 3)}
    for p in poses:
        for x in (p["t0"], p["t0"] + ramp(p), p["t1"], p["t1"] + ramp(p)):
            if 0 <= x <= dur:
                times.add(round(x, 3))
        if p["pose"] in OSC:
            k = p["t0"]
            while k < p["t1"]:
                times.add(round(k, 3))
                k += 0.125
    if moving:
        k = 0.0
        while k < dur:
            if speed(k) > 0.25:
                times.add(round(k, 3))
            k += 0.125
    for t in sorted(times):
        f = 1 + t * FPS
        sp = speed(t)
        legs_busy = any(set(POSES.get(q["pose"], {})) & {"hips", "hipL", "hipR"} for q in active(t))
        gait = sp > 0.25 and not legs_busy
        amp = min(45.0, 14 + sp * 9) if gait else 0.0
        ph = math.sin(2 * math.pi * min(2.6, 0.9 + sp * 0.35) * t)
        for j in joints:
            v = value(j, t)
            if gait and j in ("hipL", "hipR", "shL", "shR"):
                sgn = 1 if j in ("hipL", "shR") else -1
                v += sgn * amp * ph * (0.6 if j.startswith("sh") else 1.0)
            o = parts[j]
            if j == "hips":
                o.location = (0, 0, 0.52 * h + v * h)
                o.keyframe_insert("location", frame=f)
            else:
                o.rotation_euler = (math.radians(v), 0, 0)
                o.keyframe_insert("rotation_euler", frame=f)
    # 伸手 / 看：用 Damped Track 约束，影响力按时间开关
    for p in poses:
        tgt = _target_obj(p.get("target"), {}, name) if p.get("target") is not None else None
        if tgt is None:
            continue
        if p["pose"] in REACH:
            arms = {"both": ("shL", "shR"), "L": ("shL",), "R": ("shR",)}.get(p.get("arm", "R"), ("shR",))
            for a in arms:
                con = parts[a].constraints.new("DAMPED_TRACK")
                con.target, con.track_axis = tgt, "TRACK_NEGATIVE_Z"
                _key_influence(con, p["t0"], p["t1"], ramp=ramp(p))
        if p["pose"] in ("look", "nod") or p.get("look"):
            lt = _target_obj(p.get("look") or p.get("target"), {}, name)
            if lt is not None:
                con = parts["neck"].constraints.new("DAMPED_TRACK")
                con.target, con.track_axis = lt, "TRACK_NEGATIVE_Y"
                _key_influence(con, p["t0"], p["t1"], peak=0.8, ramp=ramp(p))


def _key_influence(con, t0, t1, peak=1.0, ramp=RAMP):
    for t, v in ((0, 0.0), (max(0.0, t0 - 0.01), 0.0), (t0 + ramp, peak), (t1, peak), (t1 + ramp, 0.0)):
        con.influence = v
        con.keyframe_insert("influence", frame=1 + t * FPS)


def prop_object(p):
    """道具代理：一个有尺寸和颜色的方块；keys：{t, at} 位置 / {t, attach: '人名:handR'} 拿在手里 / {t, detach: [x,y,z]} 放下 /
    {t, rot: [x,y,z]}；open："+x" 等，去掉朝这个方向的一面；swing：[{t0, t1, amp, hz}] 摆动；visible：[[t0, t1]] 只在这段时间出现。"""
    w, d, hh = p.get("size", [0.3, 0.2, 0.1])
    at = p.get("at", [0, 0, 0])
    o = box("prop_" + p["name"], at[0] - w / 2, at[0] + w / 2, at[1] - d / 2, at[1] + d / 2, at[2], at[2] + hh, tuple(p.get("color", (0.92, 0.92, 0.9))))
    if p.get("open"):  # 壳子道具（花轿、车厢、柜子）去掉一面：从开口能拍到里面的人，不会整屏一块颜色（《好日子》第 6 条镜 2）
        import bmesh
        ax = {"+x": (0, 1), "-x": (0, -1), "+y": (1, 1), "-y": (1, -1)}[p["open"]]
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.normal[ax[0]] * ax[1] > 0.9], context="FACES_ONLY")
        bm.to_mesh(o.data)
        bm.free()
        if p.get("open_at") is not None:  # 开口先挂一块帘子，tear_from 起慢慢裂开，open_at 完全打开（第 4 条扯线、冲出轿子）
            cu = box("prop_" + p["name"] + "_curtain", -0.5, 0.5, -0.5, 0.5, -0.5, 0.5, tuple(c * 0.75 for c in p.get("color", (0.92, 0.92, 0.9))))
            cu.parent = o
            sgn = 1 if p["open"][0] == "+" else -1
            cu.location = (0.5 * sgn, 0, 0) if ax[0] == 0 else (0, 0.5 * sgn, 0)
            full = (0.02, 0.92, 0.92) if ax[0] == 0 else (0.92, 0.02, 0.92)
            cu.scale = full
            cu.keyframe_insert("scale", frame=1)
            t0 = p.get("tear_from", p["open_at"] - 0.3)
            cu.keyframe_insert("scale", frame=1 + t0 * FPS)
            cu.scale = (full[0] * (0.05 if ax[0] == 1 else 1), full[1] * (0.05 if ax[0] == 0 else 1), full[2])  # 从中间往两边收
            cu.keyframe_insert("scale", frame=1 + p["open_at"] * FPS)
    base = tuple(o.location)
    off = (0, 0, hh / 2)
    con = None
    ks0 = sorted(p.get("keys", []), key=lambda k: k["t"])
    ks0 = [dict(k, t=max(0.0, k["t"] - 1.0 / FPS)) if i + 1 < len(ks0) and abs(ks0[i + 1]["t"] - k["t"]) < 1e-6 else k
           for i, k in enumerate(ks0)]  # 同一时刻两个位置：前一个提前一帧（Blender 一帧只留一个关键帧）
    for k in ks0:
        f = 1 + k["t"] * FPS
        if "attach" in k:
            tgt = _target_obj(k["attach"], {}, p["name"])
            if tgt is None:
                continue
            if con is None:
                con = o.constraints.new("COPY_LOCATION")
                con.influence = 0.0
                con.keyframe_insert("influence", frame=1)
            con.target = tgt
            con.influence = 0.0
            con.keyframe_insert("influence", frame=max(1, f - 1))
            con.influence = 1.0
            con.keyframe_insert("influence", frame=f)
        if "detach" in k:  # 放下：松手（如果拿着）并落到这个点；没拿过的道具也一样落位
            if con is not None:
                con.influence = 1.0
                con.keyframe_insert("influence", frame=max(1, f - 1))
                con.influence = 0.0
                con.keyframe_insert("influence", frame=f)
            q = k["detach"]
            o.location = (q[0], q[1], q[2] + off[2])
            o.keyframe_insert("location", frame=f)
        if "at" in k:
            q = k["at"]
            o.location = (q[0], q[1], q[2] + off[2])
            o.keyframe_insert("location", frame=f)
        if "rot" in k:
            o.rotation_euler = [math.radians(a) for a in k["rot"]]
            o.keyframe_insert("rotation_euler", frame=f)
    for s in p.get("swing", []):
        t = s["t0"]
        while t <= s["t1"]:
            o.rotation_euler = (math.radians(s.get("amp", 15) * math.sin(2 * math.pi * s.get("hz", 1.0) * (t - s["t0"]))), 0, 0)
            o.keyframe_insert("rotation_euler", frame=1 + t * FPS)
            t += 0.125
    if p.get("visible"):
        keys = [(0, 0.01)]
        for t0, t1 in p["visible"]:
            keys += [(max(0.0, t0 - 0.01), 0.01), (t0, 1.0), (t1, 1.0), (t1 + 0.01, 0.01)]
        for t, s in keys:
            o.scale = (w * s, d * s, hh * s)
            o.keyframe_insert("scale", frame=1 + t * FPS)
    _ = base
    return o


def effect(e):
    """特效代理（精准白模第二期）：只演"什么东西在什么时候往哪动、动多大"，不演质感。
    kind：debris 碎石跳 / split 裂缝张开 / fall 漫天落下的纸屑灰尘 / sweep 影子扫过地面 / reach 巨物的一根根臂伸过去（树根、触手）。
    时间 t0–t1 是这一条里的秒数；at 是中心点；颜色 color。"""
    import random
    rnd = random.Random(e.get("seed", 7))
    t0, t1 = e["t0"], e["t1"]
    col = tuple(e.get("color", (0.6, 0.6, 0.6)))
    ax, ay, az = e["at"]
    out = []
    if e["kind"] == "debris":  # 碎石一颗颗跳起来：每颗按自己的节奏上下弹
        r = e.get("radius", 0.6)
        for i in range(e.get("count", 14)):
            x, y = ax + rnd.uniform(-r, r), ay + rnd.uniform(-r, r)
            sz = rnd.uniform(0.03, 0.07) * e.get("scale", 1.0)
            o = box(f"fx_debris{i}", x - sz, x + sz, y - sz, y + sz, az, az + 2 * sz, col)
            ph, hz = rnd.uniform(0, 1), rnd.uniform(2.5, 4.5)
            t = t0
            key(o, 0, loc=(x, y, -50))  # 到点才出现：开场地上是干净的
            key(o, max(0, t0 - 0.01), loc=(x, y, -50))
            while t <= t1:
                h = abs(math.sin(math.pi * (hz * (t - t0) + ph))) * e.get("height", 0.12) * min(1.0, (t - t0) / 0.6 + 0.2)
                key(o, t, loc=(x, y, az + sz + h))
                t += 1 / 12
            out.append(o)
    elif e["kind"] == "split":  # 裂缝从一条线张开到 width 宽，高 height；深色
        w, h, d = e.get("width", 2.0), e.get("height", 10.0), e.get("depth", 0.6)
        o = box("fx_split", ax - 0.5, ax + 0.5, ay - d / 2, ay + d / 2, az, az + h, col)
        o.scale = (0.02, d, h)
        o.keyframe_insert("scale", frame=1)
        o.keyframe_insert("scale", frame=1 + t0 * FPS)
        o.scale = (w, d, h)
        o.keyframe_insert("scale", frame=1 + t1 * FPS)
        out.append(o)
    elif e["kind"] == "fall":  # 纸屑、灰从高处飘落：area [宽, 深]，height 起落高度
        wd, dp = e.get("area", [8, 8])
        for i in range(e.get("count", 60)):
            x, y = ax + rnd.uniform(-wd / 2, wd / 2), ay + rnd.uniform(-dp / 2, dp / 2)
            sz = 0.05 * e.get("scale", 1.0)
            o = box(f"fx_fall{i}", x - sz, x + sz, y - sz, y + sz, 0, 2 * sz, col)
            st = t0 + rnd.uniform(0, max(0.01, t1 - t0))
            z0 = az + e.get("height", 6) * rnd.uniform(0.6, 1.0)
            spd = rnd.uniform(0.8, 1.4) * e.get("speed", 1.0)
            dur = z0 / spd
            key(o, 0, loc=(x, y, -50))
            key(o, max(0, st - 0.01), loc=(x, y, -50))
            key(o, st, loc=(x, y, z0))
            key(o, st + dur, loc=(x + rnd.uniform(-0.6, 0.6), y + rnd.uniform(-0.6, 0.6), az))
            out.append(o)
    elif e["kind"] == "sweep":  # 巨大的影子扫过地面：贴地的深色薄板从 at 移到 to（第 9 条山神的影子）
        wd, dp = e.get("area", [30, 20])
        o = box("fx_sweep", ax - wd / 2, ax + wd / 2, ay - dp / 2, ay + dp / 2, az + 0.02, az + 0.05, col)
        tx, ty, tz = e["to"]
        key(o, 0, loc=tuple(o.location))
        key(o, t0, loc=tuple(o.location))
        key(o, t1, loc=(tx, ty, tz + 0.035))
        out.append(o)
    elif e["kind"] == "reach":  # 一根臂从 at 伸到 to：长方条沿方向生长，粗 thick
        tx, ty, tz = e["to"]
        dx, dy, dz = tx - ax, ty - ay, tz - az
        L = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        th = e.get("thick", 0.6)
        o = box("fx_reach", -th / 2, th / 2, -th / 2, th / 2, 0, 1, col)  # 底面在原点，沿 z 长
        o.location = (ax, ay, az)
        o.rotation_mode = "QUATERNION"
        from mathutils import Vector
        o.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(Vector((dx, dy, dz)).normalized())
        o.data.transform(__import__("mathutils").Matrix.Translation((0, 0, 0.5)))  # 立方体中心挪到底面以上
        o.scale = (th, th, 0.01)
        o.keyframe_insert("scale", frame=1)
        o.keyframe_insert("scale", frame=1 + t0 * FPS)
        o.scale = (th, th, L)
        o.keyframe_insert("scale", frame=1 + t1 * FPS)
        if e.get("back") is not None:  # 缩回去
            o.keyframe_insert("scale", frame=1 + e["back"][0] * FPS)
            o.scale = (th, th, 0.01)
            o.keyframe_insert("scale", frame=1 + e["back"][1] * FPS)
        out.append(o)
    return out


# ---------------- 4.0 alpha.8：巨物形状代理与光（精准白模第三期） ----------------
# 巨物（设定.json 里 kind: giant 的角色）不再是放大的关节小人：粗树干一样的身子、向外撑的根须、向上伸的枝、
# 头的位置是一个空心环（面朝 -Y，能透过它看到后面），身上缠几圈浅色布带。名字沿用 <名>_body / <名>_head，几何检查不用改。

def giant(name, color, height=60.0, band=(0.88, 0.62, 0.62)):
    h = height
    root = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(root)
    parts = []
    bpy.ops.mesh.primitive_cone_add(radius1=0.09 * h, radius2=0.05 * h, depth=0.8 * h, vertices=12, location=(0, 0, 0.4 * h))
    body = bpy.context.object
    body.name = name + "_body"
    parts.append(body)

    def limb(tag, base, ang, tilt, r, L):
        """从 base 出发，水平方位 ang（度，0 = 正前方 -Y），偏离竖直 tilt 度，长 L。"""
        from mathutils import Vector
        a, tl = math.radians(ang), math.radians(tilt)
        d = (math.sin(a) * math.sin(tl), -math.cos(a) * math.sin(tl), math.cos(tl))
        mid = tuple(base[i] + d[i] * L / 2 for i in range(3))
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=L, vertices=8, location=mid)
        o = bpy.context.object
        o.name = f"{name}_{tag}"
        o.rotation_euler = Vector(d).to_track_quat("Z", "Y").to_euler()
        parts.append(o)

    for i, ang in enumerate(range(0, 360, 51)):  # 七根根须：从树干根部斜着扎进地里，只比根部外撇一点（离中心约 0.13 倍身高），不铺满场地
        a = math.radians(ang + 20)
        limb(f"root{i}", (math.sin(a) * 0.07 * h, -math.cos(a) * 0.07 * h, 0.1 * h), ang + 20, 150, 0.018 * h, 0.11 * h)
    for i, ang in enumerate((65, -65, 140, -140)):  # 四根向上斜伸的枝，避开正前方，不挡空心头
        limb(f"branch{i}", (math.sin(math.radians(ang)) * 0.04 * h, -math.cos(math.radians(ang)) * 0.04 * h, 0.6 * h), ang, 48, 0.018 * h, 0.38 * h)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.075 * h, minor_radius=0.024 * h, major_segments=24, minor_segments=8,
                                     location=(0, 0, 0.9 * h), rotation=(math.pi / 2, 0, 0))  # 环面朝 -Y：正面看是一个洞；中心在 0.9 倍身高，和 head() 的注视点一致
    head = bpy.context.object
    head.name = name + "_head"
    parts.append(head)
    for o in parts:
        o.color = (*color, 1)
    for i, z in enumerate((0.22, 0.41, 0.58)):  # 缠在身上的旧红绸（褪成粉白）
        r = 0.09 * h - (0.04 * h) * (z / 0.8) + 0.005 * h
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=0.025 * h, vertices=12, location=(0, 0, z * h))
        o = bpy.context.object
        o.name = f"{name}_band{i}"
        o.color = (*band, 1)
        parts.append(o)
    for o in parts:
        o.parent = root
    return root


def giant_head(name, color, h, keys):
    """巨物俯身：keys 里写了 head（世界坐标）的时刻，空心头落到那个点（"空洞停在她面前"），
    脖子从树干顶拉到头上（拉伸约束），其余时刻头回到 0.9 倍身高。keys 与人物 keys 同格式。"""
    head = bpy.data.objects[name + "_head"]
    root = bpy.data.objects[name]
    head.parent = None
    for k in keys:
        x, y, z = k["pos"]
        hp = k.get("head") or (x, y, z + 0.9 * h)
        key(head, k["t"], loc=tuple(hp), rot=(90, 0, k.get("face", 0)))
    base = bpy.data.objects.new(name + "_neckbase", None)
    bpy.context.collection.objects.link(base)
    base.parent = root
    base.location = (0, 0, 0.78 * h)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.03 * h, depth=1, vertices=10)
    neck = bpy.context.object
    neck.name = name + "_neck"
    neck.color = (*color, 1)
    for v in neck.data.vertices:  # 圆柱轴改成 +Y、底在原点：拉伸约束沿 Y 拉长
        v.co = (v.co.x, v.co.z + 0.5, v.co.y)
    cl = neck.constraints.new("COPY_LOCATION")
    cl.target = base
    top = bpy.data.objects.new(name + "_necktop", None)  # 脖子接在环的顶上，不穿过空洞（《好日子》第 12 条镜 4）
    bpy.context.collection.objects.link(top)
    top.parent = head
    top.location = (0, 0.075 * h, 0)  # 环躺在局部 XY 平面，转 90° 后局部 +Y 朝上
    st = neck.constraints.new("STRETCH_TO")
    st.target = top
    st.rest_length = 1.0
    st.volume = "NO_VOLUME"
    return head


# 光型：elev 太阳高度角（度）、energy 强度、sun 光色、sky 天空色（背景和环境光）、sky_strength。
# from 是光照来的水平方向 [x, y]（场景坐标，默认左前方）；落日、清晨的影子最长，正午几乎没有影子。
# sky_view：镜头直接看到的天空亮度是环境光的几倍（默认 1.6）：天比地亮，远景的山顶不会和天糊成一片，物体也不会被环境光冲白。
LIGHTS = {
    "noon":     dict(elev=78, energy=4.5, sun=(1.0, 0.98, 0.94), sky=(0.66, 0.74, 0.84), sky_strength=0.75),
    "dawn":     dict(elev=16, energy=3.0, sun=(1.0, 0.95, 0.86), sky=(0.72, 0.74, 0.78), sky_strength=0.6),
    "sunset":   dict(elev=9, energy=3.0, sun=(1.0, 0.66, 0.38), sky=(0.95, 0.62, 0.42), sky_strength=0.7),
    "dusk":     dict(elev=22, energy=2.2, sun=(0.82, 0.86, 1.0), sky=(0.50, 0.54, 0.66), sky_strength=0.5),
    "overcast": dict(elev=60, energy=0.6, sun=(1.0, 1.0, 1.0), sky=(0.62, 0.64, 0.67), sky_strength=1.0),
    "night":    dict(elev=40, energy=2.2, sun=(0.72, 0.8, 1.0), sky=(0.24, 0.28, 0.4), sky_strength=0.55),  # 白模是结构参考：夜里也要读得清，只留冷色
}


def apply_light(spec_light):
    """有 light 时换 EEVEE + 太阳灯：物体色照旧（材质读物体的 color），影子方向和长度、天空色按光型。没有 light 时保持原来的 Workbench 平光。"""
    from mathutils import Vector
    sc = bpy.context.scene
    preset = dict(LIGHTS[spec_light.get("preset", "noon")])
    preset.update({k: v for k, v in spec_light.items() if k in preset})
    try:
        sc.render.engine = "BLENDER_EEVEE"
    except TypeError:
        sc.render.engine = "BLENDER_EEVEE_NEXT"
    try:
        sc.eevee.taa_render_samples = 8
    except AttributeError:
        pass
    sc.view_settings.view_transform = "Standard"  # 识别色不被 AgX 压灰
    m = bpy.data.materials.new("previs_objcolor")
    m.use_nodes = True
    nt = m.node_tree
    bs = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    at = nt.nodes.new("ShaderNodeAttribute")
    at.attribute_type = "OBJECT"
    at.attribute_name = "color"
    nt.links.new(at.outputs["Color"], bs.inputs["Base Color"])
    bs.inputs["Roughness"].default_value = 0.9
    for o in bpy.data.objects:
        if o.type == "MESH":
            o.data.materials.clear()
            o.data.materials.append(m)
    w = sc.world
    w.use_nodes = True
    nt = w.node_tree
    bg = next(n for n in nt.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = (*preset["sky"], 1)
    bg.inputs["Strength"].default_value = preset["sky_strength"]
    out = next(n for n in nt.nodes if n.type == "OUTPUT_WORLD")
    seen = nt.nodes.new("ShaderNodeBackground")  # 镜头看到的天空
    seen.inputs["Color"].default_value = (*preset["sky"], 1)
    seen.inputs["Strength"].default_value = preset["sky_strength"] * spec_light.get("sky_view", 1.6)
    lp = nt.nodes.new("ShaderNodeLightPath")
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs["Fac"])
    nt.links.new(bg.outputs["Background"], mix.inputs[1])
    nt.links.new(seen.outputs["Background"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    ld = bpy.data.lights.new("previs_sun", "SUN")
    ld.energy, ld.color, ld.angle = preset["energy"], preset["sun"], math.radians(1.5)
    sun = bpy.data.objects.new("previs_sun", ld)
    bpy.context.collection.objects.link(sun)
    fx, fy = spec_light.get("from", (-1.0, -0.6))
    n = math.hypot(fx, fy) or 1.0
    el = math.radians(preset["elev"])
    d = Vector((-fx / n * math.cos(el), -fy / n * math.cos(el), -math.sin(el)))  # 光传播的方向：从 from 那边照过来、向下
    sun.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    return sun


# ---------------- 4.0 alpha.12：挂载机位（主观镜头、车内、车头）与按时间隐藏 ----------------

HIDE = {}  # 物体名 → 要藏起来的时间段 [(t0, t1)]；flush_hidden() 一次性打关键帧（同一个人连着几个主观镜头都要藏，分开打会互相覆盖）


def key_hidden(objs, t0, t1, only=False):
    """这些物体在 [t0, t1) 秒里不渲染、不参与射线（白模机检看不到它）；only=True 反过来：只在这段时间出现。"""
    for o in objs:
        iv = HIDE.setdefault(o.name, [])
        if only:
            iv += [(-1.0, t0), (t1, 10.0 ** 5)]
        else:
            iv.append((t0, t1))


def flush_hidden():
    for name, iv in HIDE.items():
        o = bpy.data.objects.get(name)
        if o is None:
            continue
        merged = []
        for a, b in sorted(iv):
            if merged and a <= merged[-1][1] + 1e-6:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        keys = {1: any(a <= 0 < b for a, b in merged)}
        for a, b in merged:
            fa, fb = int(round(1 + a * FPS)), int(round(1 + b * FPS))
            if fa > 1:
                keys.setdefault(fa - 1, False)
                keys[fa] = True
            if fb < 10 ** 6:
                keys.setdefault(fb - 1, True)
                keys[fb] = False
        for attr in ("hide_render", "hide_viewport"):
            for f, v in sorted(keys.items()):
                setattr(o, attr, v)
                o.keyframe_insert(attr, frame=f)
            ad = o.animation_data
            try:
                curves = ad.action.fcurves
            except AttributeError:
                curves = [x for layer in ad.action.layers for strip in layer.strips for bag in strip.channelbags for x in bag.fcurves]
            for fc in curves:
                if fc.data_path == attr:
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'CONSTANT'
    HIDE.clear()


def owned(name):
    """名字是 name 或以 name_ 开头的网格（一个人的全身、一个道具和它的帘子）。"""
    return [o for o in bpy.data.objects if o.type == "MESH" and (o.name == name or o.name.startswith(name + "_"))]


def mount_camera(s, c, t0, t1, aims):
    """s.mount：人名（挂在头上）或 'prop_红车'（挂在车身中心）；mount_off 偏移；looks：[{t, yaw, pitch}] 视线；
    hide：这一镜里藏起来的人或道具（主观者本人、镜头所在的那辆车）；hood：车内镜头的引擎盖；bob：走跑时上下颠的幅度（米）。"""
    m = s["mount"]
    tgt = bpy.data.objects.get(m + "_head") if not m.startswith("prop_") else (aims.get(m) or bpy.data.objects.get(m))
    mt = bpy.data.objects.new(c.name + "_mount", None)
    bpy.context.collection.objects.link(mt)
    mt.location = tuple(s.get("mount_off", (0, 0, 0)))
    if tgt is not None:
        cl = mt.constraints.new("COPY_LOCATION")
        cl.target, cl.use_offset = tgt, True
    c.parent = mt
    pv = bpy.data.objects.new(c.name + "_pivot", None)  # 视线：绕竖轴转的支点，转多少度就是 yaw（和人的 face 同一套角度）
    bpy.context.collection.objects.link(pv)
    pv.parent = mt
    lt = bpy.data.objects.new(c.name + "_look", None)
    bpy.context.collection.objects.link(lt)
    lt.parent = pv
    looks = s.get("looks") or [{"t": t0, "yaw": 0.0, "pitch": 0.0}]
    prev = None
    for L in looks:
        y = L["yaw"]
        if prev is not None:  # 走最短的那一圈：回头 170° 不会绕成 190°
            while y - prev > 180:
                y -= 360
            while y - prev < -180:
                y += 360
        prev = y
        key(pv, max(t0, L["t"]), rot=(0, 0, y))
        key(lt, max(t0, L["t"]), loc=(0, -10.0, 10.0 * math.tan(math.radians(L.get("pitch", 0.0)))))
    con = c.constraints.new("TRACK_TO")
    con.target, con.track_axis, con.up_axis = lt, "TRACK_NEGATIVE_Z", "UP_Y"
    bob = s.get("bob", 0.0)
    k = t0
    while k <= t1 + 1e-6:  # 走跑时上下颠：每步一次
        key(c, k, loc=(0, 0, bob * math.sin(2 * math.pi * 2.6 * (k - t0)) if bob else 0.0))
        k += 1.0 / 12 if bob else max(t1 - t0, 0.1)
    for o in (pv, lt):  # 转头有加速减速，不是匀速摇
        ease_all([o], paths=("location", "rotation_euler"))
    hidden = []
    for n in s.get("hide", []):
        hidden += owned(n)
    if hidden:
        key_hidden(hidden, t0, t1)
    if s.get("hood"):  # 车内：一块引擎盖，告诉模型"我在车里往前看"
        hd = s["hood"]
        w, L, h = hd["size"]
        o = box("fx_hood_" + c.name, -w / 2, w / 2, -L / 2, L / 2, -h / 2, h / 2, tuple(hd.get("color", (0.85, 0.2, 0.15))))
        o.parent = mt
        o.location = tuple(hd["off"])
        o.rotation_euler = (0, 0, math.radians(hd.get("yaw", 0.0) - 180))
        key_hidden([o], t0, t1, only=True)
