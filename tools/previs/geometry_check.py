#!/usr/bin/env python3
"""零抽卡闸门 · 白模几何检查：在 Blender 里搭出白模场景，按镜头规格单逐镜检查构图（docs/14 第四节）。

用法（不用开 Blender 界面）：
  python3 tools/previs/geometry_check.py <白模规格.json> <镜头规格单.json> [--out 报告.md]

每镜在 25%、50%、75% 三个时刻取样，检查：
  入画   "画面里"写到的主体（白模里同名的人物）头部在不在画面里
  大小   主体从头到脚占画高多少；< 6% 判"太小，模型会画成小点"
  遮挡   从镜头到主体头部的视线被什么挡住；写了"虚化 / 背影 / 肩膀"的前景人物是有意遮挡，只记录
  头后   主体头部正后方有没有塔、针、柱子这类竖直物体（会和人融成一体，第一版的老问题）
  穿墙   镜头位置是不是落在墙、道具的方块里面
  轴线   规格单写了 "axis": {"a": 人物, "b": 人物} 时，记录每镜机位在轴线哪一侧，两侧都出现就提醒越轴
  身份   （4.0 alpha.12，学自 scenario-labs blockout skill）人物身边立着的零散方块会被模型读成多出来的人；
         两个人的身子叠在一起会被读成一个人或长出多余的肢体；头钻进墙里、家具里会被画成断头
  机位高度（只统计）  每镜机位比主体眼睛高或低多少；不是人眼高度的机位要有理由（俯视 = 压迫，仰视 = 威严）
退出码 0 = 无 FAIL。
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if "bpy" not in sys.modules and not os.environ.get("_GEOMETRY_IN_BLENDER"):
    sys.path.insert(0, HERE)
    from render_previs import blender_bin  # noqa: E402
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("previs")
    ap.add_argument("shotspec")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or os.path.splitext(a.shotspec)[0] + "_几何检查.md"
    env = dict(os.environ, _GEOMETRY_IN_BLENDER="1")
    r = subprocess.run([blender_bin(), "-b", "--factory-startup", "-P", os.path.abspath(__file__), "--",
                        os.path.abspath(a.previs), os.path.abspath(a.shotspec), os.path.abspath(out)], env=env,
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        sys.exit("Blender 运行失败：\n" + (r.stderr[-2000:] or r.stdout[-2000:]))
    txt = open(out, encoding="utf-8").read()
    print(txt)
    sys.exit(1 if "| FAIL" in txt else 0)

# ---------------- 以下在 Blender 里运行 ----------------
import runpy  # noqa: E402

import bpy  # noqa: E402
from bpy_extras.object_utils import world_to_camera_view  # noqa: E402
from mathutils import Vector  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:]
PREVIS, SHOTSPEC, OUT = argv[0], argv[1], argv[2]
tmp = os.path.join(os.path.dirname(OUT), "_geo_tmp")
sys.argv = [sys.argv[0], "--", PREVIS, tmp, "--no-render"]
runpy.run_path(os.path.join(HERE, "build_from_spec.py"), run_name="__main__")
shutil.rmtree(tmp, ignore_errors=True)

sc = bpy.context.scene
FPS = sc.render.fps
spec = json.load(open(SHOTSPEC, encoding="utf-8"))
PV = json.load(open(PREVIS, encoding="utf-8"))
GIANTS = {p["name"] for p in PV.get("people", []) if p.get("shape") == "giant"}
markers = sorted(((m.frame - 1) / FPS, m.camera) for m in sc.timeline_markers)
people = {o.name for o in bpy.data.objects if o.type == "EMPTY" and bpy.data.objects.get(o.name + "_head")}
boxes = [o for o in bpy.data.objects if o.type == "MESH" and not any(o.name.startswith(p + "_") for p in people)
         and not o.name.startswith("fx_")]  # 特效代理（碎石、纸屑、裂缝、巨物的臂）是要拍的东西，不当障碍
TALL = [o for o in boxes if o.dimensions.z > 6 * max(o.dimensions.x, o.dimensions.y, 1e-6) and o.dimensions.z > 3]


def cam_at(t):
    cur = None
    for t0, c in markers:
        if t0 <= t + 1e-6:
            cur = c
    return cur


def inside(o, p):
    if o.hide_viewport:  # 这一镜里藏起来的（主观者本人、镜头所在的那辆车）
        return False
    mn = Vector(min(v[i] for v in (o.matrix_world @ Vector(c) for c in o.bound_box)) for i in range(3))
    mx = Vector(max(v[i] for v in (o.matrix_world @ Vector(c) for c in o.bound_box)) for i in range(3))
    if not all(mn[i] + 1e-4 < p[i] < mx[i] - 1e-4 for i in range(3)):
        return False
    if len(o.data.vertices) <= 8:  # 方块：包围盒就是它本身
        return True
    # 圆柱、圆锥、环（巨物的根须和枝是斜的，包围盒比本体大得多）：看离点最近的面，点在面的背面才算在里面
    inv = o.matrix_world.inverted()
    ok, loc, nrm, _i = o.closest_point_on_mesh(inv @ p)
    return bool(ok) and (inv @ p - loc).dot(nrm) < 0


def owner(obj):
    for p in people:
        if obj.name == p or obj.name.startswith(p + "_"):
            return p
    return obj.name


def subjects(body):
    """从"画面里：……。"取主体。镜子里、画像里的人不算；只露手脚的特写不查头；虚化、背影、背对镜头的算前景。"""
    m = re.search(r"画面里：([^。]*)。", body)
    if not m:
        return [], [], []
    main, fg, partial = [], [], []
    for item in re.split(r"[、，]", m.group(1)):
        if re.search(r"镜子里|画像里|铜镜里", item):
            continue
        for p in people:
            if p in item:
                if re.search(r"的手|的双手|的右手|的左手|的拳头|的脚|光着的脚|的鼓面", item):
                    partial.append(p)
                elif re.search(r"虚化|背影|肩膀|后脑|背对", item):
                    fg.append(p)
                else:
                    main.append(p)
    return main, fg, partial


NOT_HAND = {"sit", "sit_ground", "kneel", "kowtow", "look", "nod", "run", "walk", "laugh", "bend", "crouch", "dragged", "struggle", "pick", "lean_back", "fall"}


def focus_points(sh, main, partial, tl):
    """这一镜真正要拍到的点：主体的头；只露手脚的特写看手或脚；插入镜头里写明了手、脚、鞋的，看节拍里做动作那个人的手或脚。
    手常常是伸进画面来的，只在这个人的节拍进行中（tl 是镜内秒数）查；脚整镜都查。"""
    pts = []
    for p in main:
        pts.append((p, bpy.data.objects[p + "_head"].matrix_world.translation.copy()))
    st = sh.get("struct", {})
    text = st.get("object", "") + " ".join(a.get("zh", "") for a in st.get("action", []))
    who = list(partial)
    if st.get("type") == "insert" and re.search(r"脚|鞋|手", text):
        who += [b["who"] for b in st.get("beats") or [] if b.get("who") in people]
    feet = re.search(r"脚|鞋", st.get("object", "")) or (re.search(r"光脚|抬脚", text) and "的手" not in sh.get("body", "")[:30])
    for p in dict.fromkeys(who):
        if feet:
            pts.append((p, bpy.data.objects[p].matrix_world.translation + Vector((0, 0, 0.05))))
        elif any(b.get("who") == p and b.get("do") not in NOT_HAND and b["t"] + 0.4 <= tl <= (sh["t1"] - sh["t0"] if b.get("hold") else b["t"] + b.get("dur", 1.5))
                 for b in st.get("beats") or []):
            h = bpy.data.objects.get(p + "_handR")
            if h:
                pts.append((p, h.matrix_world.translation.copy()))
    return pts


def ignorable(ob, mine):
    """mine 里的物件整个不算前景；mine 里的人只有手和胳膊不算（递东西、攥手腕时伸进画面的手），身子挡住了照样算。
    名字以 * 开头的是这一镜的焦点人物，整个人都不算。"""
    k = owner(ob)
    if k.startswith("fx_") or (ob.type == "MESH" and ob.dimensions.z < 0.06 and max(ob.dimensions.x, ob.dimensions.y) > 2):  # 路面、地毯这类贴地的薄层算地面
        return True
    if "*" + k in mine or (k in mine and k not in people):
        return True
    return k in mine and ("_hand" in ob.name or "_arm" in ob.name)


def frame_cover(cam, cp, pts, mine):
    """从镜头往画面 14×8 个点打射线：落在主体前面（比主体近 0.25 米以上）的东西，各占画面多少。
    地面、主体自己不算。查的是"画面大半被前景占了"——整屏红轿壁、满屏抬轿人的腿（《好日子》第 6 条镜 2、镜 8）。"""
    if not pts:
        return {}
    dist = min((q - cp).length for _p, q in pts)
    fr = [cam.matrix_world @ v for v in cam.data.view_frame(scene=sc)]  # 右上、右下、左下、左上
    nx, ny, cnt = 14, 8, {}
    for i in range(nx):
        for j in range(ny):
            u, w = (i + 0.5) / nx, (j + 0.5) / ny
            top = fr[3].lerp(fr[0], u)
            bot = fr[2].lerp(fr[1], u)
            d = (top.lerp(bot, w) - cp).normalized()
            hit, loc, _n, _i, ob, _m = sc.ray_cast(dg, cp, d, distance=max(dist * 0.5, min(dist * 0.9, dist - 0.25)))
            if not hit or ob.name.endswith("_Floor") or ignorable(ob, mine):
                continue
            k = owner(ob)
            cnt[k] = cnt.get(k, 0) + 1
    return {k: v / (nx * ny) for k, v in cnt.items()}


import math  # noqa: E402
rows, axis_sides, comp, side = [], {}, [], []  # comp：构图统计（不拦，docs/21 第五节）；side：两人同框侧拍（不拦）
dg = bpy.context.evaluated_depsgraph_get()
for sh in spec["shots"]:
    L = sh["t1"] - sh["t0"]
    main, fg, partial = subjects(sh["body"])
    ok_occ = set(sh.get("allow_occlude", []))
    notes = []
    level = "PASS"
    main_all = list(main)
    for frac in (0.25, 0.5, 0.75):
        t = sh["t0"] + frac * L
        main = [p for p in main_all if t - sh["t0"] >= sh.get("enter", {}).get(p, 0)]  # 长镜头里中途才入画的人，入画之前不查
        sc.frame_set(int(round(1 + t * FPS)))
        dg = bpy.context.evaluated_depsgraph_get()
        cam = cam_at(t)
        if cam is None:
            continue
        sc.camera = cam
        cp = cam.matrix_world.translation
        for o in boxes:
            if inside(o, cp):
                notes.append(f"{t:.1f}s 镜头在「{o.name}」里面")
                level = "FAIL"
        for o in [x for x in bpy.data.objects if x.type == "MESH" and owner(x) in people]:  # 镜头钻进人的身体里，整幅画面是一块颜色（《好日子》第 1 条镜 4）
            if inside(o, cp):
                notes.append(f"{t:.1f}s 镜头在「{owner(o)}」的身体里")
                level = "FAIL"
                break
        pts = focus_points(sh, main, partial, t - sh["t0"])
        ppts = [(n, bpy.data.objects[n].matrix_world.translation.copy()) for n in sh.get("see", [])
                if bpy.data.objects.get(n) is not None and not bpy.data.objects[n].hide_viewport]  # 画面里要拍到的车和道具
        mine = {"*" + p for p, _q in pts + ppts}
        st_ = sh.get("struct", {})
        if st_.get("type") == "insert":  # 特写里做动作的人、被拿的道具、动作的对象都是主体，不算前景
            for b in st_.get("beats") or []:
                mine |= {b.get("who"), "prop_" + b["prop"] if b.get("prop") else None, str(b.get("target", "")).split(".")[0]}
            mine |= set(partial)
        if not pts and st_.get("type") == "insert":  # 拍物件的特写：物件就在镜头对准的点上（第 3 条镜 3 轿帘被腿和轿壁占满）
            pv = [x for x in PV.get("shots", []) if x.get("start", 0) <= t + 1e-6]
            if pv and pv[-1].get("look_at"):
                la = pv[-1]["look_at"]
                q = bpy.data.objects[la].matrix_world.translation.copy() if isinstance(la, str) else Vector(la)
                pts = [("_object", q)]
                mine |= {o.name for o in boxes if inside(o, q) or o.name.startswith("代理_")}  # 对准点所在的物件（轿帘就是花轿的一面）、特写的代理块都是主体
        for n, q in ppts:  # 只有车的镜头也要查：车在不在画里、有没有被挡、是不是小得看不见（高难镜头测试：整屏护栏照样 PASS）
            ob = bpy.data.objects[n]
            vq = world_to_camera_view(sc, cam, q)
            if not (0 <= vq.x <= 1 and 0 <= vq.y <= 1 and vq.z > 0):
                if not (n in sh.get("allow_exit", []) and frac >= 0.5):
                    notes.append(f"{t:.1f}s「{n[5:]}」不在画面里")
                    level = "FAIL"
                continue
            d = q - cp
            hit, hl, _n, _i, hob, _m = sc.ray_cast(dg, cp, d.normalized(), distance=d.length)
            if hit and owner(hob) != n and not hob.name.startswith(n + "_") and not hob.name.endswith("_Floor") and owner(hob) not in ok_occ \
                    and (hl - cp).length < d.length - max(ob.dimensions) / 2:
                notes.append(f"{t:.1f}s「{n[5:]}」被「{owner(hob)}」挡住")
                level = "FAIL"
            cs = [ob.matrix_world @ Vector(c_) for c_ in ob.bound_box]
            ys = [world_to_camera_view(sc, cam, c_).y for c_ in cs]
            if max(ys) - min(ys) < 0.03 and n not in sh.get("allow_small", []):
                notes.append(f"{t:.1f}s「{n[5:]}」只占画高 {max(ys) - min(ys):.0%}，看不清")
                level = "FAIL" if level == "FAIL" else "WARN"
        cov = frame_cover(cam, cp, pts + ppts, mine)
        tot = sum(cov.values())
        # 人和道具挡住三成就算；布景（门框、镜框、墙）做前景框可以多占一些，过肩人物最多占一半
        big = [k for k, v in cov.items() if v > (0.5 if k in fg else 0.3 if ((k in people and k not in GIANTS) or k.startswith("prop_")) else 0.45)]  # 巨物在画面里是布景的分量
        if tot > 0.55 or big:  # 过肩前景可以占一侧，但不能占掉半个画面；allow_occlude 不豁免这一条
            worst = max(cov, key=cov.get)
            notes.append(f"{t:.1f}s 前景占了画面 {tot:.0%}（最多的是「{worst}」{cov[worst]:.0%}），主体没拍到：换机位或把前景挪开")
            level = "FAIL"
        for p, q in pts:  # 主体自己太大：特写里头塞满了画面
            vq = world_to_camera_view(sc, cam, q)
            if p not in main and p != "_object":  # 手、脚特写：那只手或脚要在画里，而且射线先打到的是它自己（第 6 条镜 8：抬轿人的腿挡住了脚）
                part = "脚" if (q - bpy.data.objects[p].matrix_world.translation).length < 0.1 else "手"
                if not (0 <= vq.x <= 1 and 0 <= vq.y <= 1 and vq.z > 0):
                    notes.append(f"{t:.1f}s「{p}」的{part}不在画面里")
                    level = "FAIL"
                    continue
                d = q - cp
                hit, hl, _n, _i, ob, _m = sc.ray_cast(dg, cp, d.normalized(), distance=d.length)
                if hit and (hl - cp).length < d.length - 0.12 and not ignorable(ob, mine) and owner(ob) not in ok_occ and not ob.name.endswith("_Floor"):
                    notes.append(f"{t:.1f}s「{p}」的{part}被「{owner(ob)}」挡住")
                    level = "FAIL"
            if p in main and vq.z > 0 and "大特写" not in sh.get("body", ""):
                r = 0.12 * 1.65  # 头的半径（rig 的头球）
                hd = bpy.data.objects[p + "_head"]
                r = max(hd.dimensions) / 2 if hd.dimensions.length > 0 else r
                up = world_to_camera_view(sc, cam, q + cam.matrix_world.to_3x3() @ Vector((0, r, 0)))
                if abs(up.y - vq.y) * 2 > 0.9:
                    notes.append(f"{t:.1f}s「{p}」的头占了画高 {abs(up.y - vq.y) * 2:.0%}，特写也要留出下巴和肩：镜头退后或换短焦")
                    level = "FAIL" if level == "FAIL" else "WARN"
        if frac == 0.5 and main and sh.get("struct", {}).get("type") in ("single", "ots", "track"):
            hd0 = bpy.data.objects[main[0] + "_head"].matrix_world.translation
            v0 = world_to_camera_view(sc, cam, hd0)
            if v0.z > 0:
                eye = abs(cp.z - hd0.z) < 0.35
                mid = 0.43 <= v0.x <= 0.57
                comp.append((sh, main[0], v0.x, eye, mid))
        for p in main:
            head = bpy.data.objects[p + "_head"].matrix_world.translation
            body = bpy.data.objects[p + "_body"]
            v = world_to_camera_view(sc, cam, head)
            if not (0 <= v.x <= 1 and 0 <= v.y <= 1 and v.z > 0):
                if p in sh.get("allow_exit", []) and frac >= 0.5:  # 跑出画面是这一镜要的（"扭头就跑"）：后半段出画不算
                    continue
                notes.append(f"{t:.1f}s「{p}」的头不在画面里")
                level = "FAIL"
                continue
            top = body.matrix_world @ Vector((0, 0, body.dimensions.z / 2))
            feet = bpy.data.objects[p].matrix_world.translation  # 人的根节点在脚底（关节小人的 _body 只是躯干）
            hgt = abs(world_to_camera_view(sc, cam, head).y - world_to_camera_view(sc, cam, feet).y)
            if hgt < 0.06 and p not in sh.get("allow_small", []):
                notes.append(f"{t:.1f}s「{p}」只占画高 {hgt:.0%}，太小")
                level = "FAIL" if level == "FAIL" else "WARN"
            d = head - cp
            hit, loc, _n, _i, obj, _m = sc.ray_cast(dg, cp, d.normalized(), distance=d.length)
            if hit and owner(obj) != p and owner(obj) not in ok_occ and not obj.name.startswith("fx_") and (loc - cp).length < d.length - 0.05:
                who = owner(obj)
                if who in fg:  # 前景过肩可以挡住画面一角，但挡住了主体的头就是没拍到（《好日子》第 2 条镜 4：站着的人挡住坐着的人）
                    notes.append(f"{t:.1f}s「{p}」的头被前景「{who}」挡住：过肩只该挡住画面一侧，把机位往侧面挪（lat）或抬高")
                    level = "FAIL" if level == "FAIL" else "WARN"
                else:
                    notes.append(f"{t:.1f}s「{p}」被「{who}」挡住")
                    level = "FAIL" if level == "FAIL" else "WARN"
            for o in [x for x in boxes if x.name not in ok_occ and max(x.dimensions.x, x.dimensions.y) > 0.15
                      and x.dimensions.z > 6 * max(x.dimensions.x, x.dimensions.y) and x.dimensions.z > 3]:
                c = o.matrix_world.translation
                ov = world_to_camera_view(sc, cam, c)
                xs = [world_to_camera_view(sc, cam, o.matrix_world @ Vector(cc)).x for cc in o.bound_box]
                # 只看离主体不远的竖直物体：远处的人类尺度家具在微距焦外，不会和主体融在一起
                if min(xs) - 0.01 <= v.x <= max(xs) + 0.01 and v.z < ov.z < v.z * 6:
                    notes.append(f"{t:.1f}s「{p}」头后是竖直物体「{o.name}」，会融在一起")
                    level = "FAIL" if level == "FAIL" else "WARN"
        for p in fg:  # 过肩的前景人物至少要有肩膀在画里，否则过肩就成了单人镜头（《天劫外包》第 6 条、样例第 1 条教训）
            hd = bpy.data.objects[p + "_head"].matrix_world.translation
            sh_pt = hd - Vector((0, 0, 0.3))
            vs = [world_to_camera_view(sc, cam, x) for x in (hd, sh_pt)]
            if not any(-0.05 <= w.x <= 1.05 and -0.1 <= w.y <= 1.05 and w.z > 0 for w in vs):
                notes.append(f"{t:.1f}s 前景「{p}」不在画里，过肩没过上")
                level = "FAIL"
        ax = spec.get("axis")
        both = ax and all(n in main + fg for n in (ax.get("a"), ax.get("b")))
        if both and ax.get("a") in people and ax.get("b") in people and frac == 0.5:
            pa = bpy.data.objects[ax["a"]].matrix_world.translation
            pb = bpy.data.objects[ax["b"]].matrix_world.translation
            cross = (pb.x - pa.x) * (cp.y - pa.y) - (pb.y - pa.y) * (cp.x - pa.x)
            off = abs(cross) / max((pb - pa).xy.length, 1e-6)
            # 机位离轴线很近（正打、正反打的正面）不算哪一侧
            if "人类视角" not in sh["name"] and not sh.get("new_axis") and off > 1.5:
                axis_sides[sh["id"]] = "左" if cross > 0 else "右"
        # 两人同框侧拍（2026-11-07，docs/14 第三节 2.5）：中景以上两人同框，机位视线和两人连线接近 90° 就是正侧面，只统计不拦
        duo = [n for n in main if n in people and n not in GIANTS]
        if frac == 0.5 and len(duo) >= 2 and (sh.get("struct", {}).get("type") == "wide" or re.search(r"中景|全景", sh.get("body", ""))) and not re.search(r"近景|特写", sh.get("body", "").split("；")[0]):
            pa = bpy.data.objects[duo[0]].matrix_world.translation
            pb = bpy.data.objects[duo[1]].matrix_world.translation
            ab, mid = (pb - pa).xy, ((pa + pb) / 2 - cp).xy
            if ab.length > 0.3 and mid.length > 0.3:
                ang = math.degrees(math.acos(max(-1.0, min(1.0, abs(ab.dot(mid)) / (ab.length * mid.length)))))
                if ang > 75:
                    side.append((sh, duo[0], duo[1], ang))
    # 精准白模（4.0 alpha.8）：每个节拍发生后 0.3 秒，做动作的人（伸手类看手）或道具必须在画面里、不被挡住
    for bt in sh.get("struct", {}).get("beats") or []:
        t = sh["t0"] + bt["t"] + 0.5  # 动作起势 0.4 秒之后再看
        if t > sh["t1"]:
            t = sh["t1"] - 0.05
        sc.frame_set(int(round(1 + t * FPS)))
        dg = bpy.context.evaluated_depsgraph_get()
        cam = cam_at(t)
        if cam is None:
            continue
        sc.camera = cam
        cp = cam.matrix_world.translation
        if bt.get("do") == "hide" or bt.get("unseen") or not (bt.get("text") or bt.get("prop")):
            continue  # 只查写进提示词的节拍和道具：没写 text 的是背景表演（坐着、扶着），不要求每一镜都拍到
        insert = sh.get("struct", {}).get("type") == "insert"
        if bt.get("prop"):
            cands, label = [bpy.data.objects.get("prop_" + bt["prop"])], bt["prop"]
        else:
            hand = "_handL" if bt.get("arm") == "L" else "_handR"
            cands = [bpy.data.objects.get(bt["who"] + hand)] + ([] if insert else [bpy.data.objects.get(bt["who"] + "_head")])
            label = bt["who"]
        cands = [o for o in cands if o is not None]
        if not cands:
            continue
        seen = [o for o in cands if (lambda w: 0 <= w.x <= 1 and 0 <= w.y <= 1 and w.z > 0)(world_to_camera_view(sc, cam, o.matrix_world.translation))]
        loc = (seen or cands)[0].matrix_world.translation
        if not seen:
            notes.append(f"{t:.1f}s 节拍「{label} {bt.get('do')}」不在画面里")
            level = "FAIL"
            continue
        d = loc - cp
        hit, hl, _n, _i, ob, _m = sc.ray_cast(dg, cp, d.normalized(), distance=d.length)
        if hit and (hl - cp).length < d.length - 0.08 and owner(ob) not in ok_occ and not ob.name.startswith(label) and not ob.name.startswith(("prop_", "fx_")) \
                and not (owner(ob) == str(bt.get("target", "")).split(".")[0] and ("_hand" in ob.name or "_arm" in ob.name)):  # 攥住对方的手腕：对方的手不算挡
            notes.append(f"{t:.1f}s 节拍「{label} {bt.get('do')}」被「{owner(ob)}」挡住")
            level = "FAIL" if level == "FAIL" else "WARN"
    rows.append((sh, level, sorted(set(notes), key=notes.index)))

# 速度：白模是 @视频1，模型照着它的动作节奏生成；人走得像快进，成片也会急、会抽（《好日子》第 4 条抬轿人 0.8 秒冲了 13 米）。
# 画面里能看到的每一段移动：走 ≤ 2 米/秒，跑（run 姿态）≤ 5 米/秒，年纪大的人（年龄写在 speed_cap）按各自上限；切点处 0.15 秒以内的跳位不算（换了时空）。
import math  # noqa: E402
cut_ts = sorted(x["start"] for x in PV["shots"])[1:]
caps = spec.get("speed_caps", {})


def _running(p, ta, tb):
    return any(q.get("pose") == "run" and q["t0"] < tb and q["t1"] > ta for q in p.get("poses", []))


movers = [(p["name"], sorted(((k["t"], k["pos"]) for k in p["keys"]), key=lambda x: x[0]), p) for p in PV.get("people", []) if not p.get("ride")]  # 坐车的人跟着车走，不查
movers += [("prop_" + pr["name"], sorted(((k["t"], k["at"]) for k in pr.get("keys", []) if "at" in k), key=lambda x: x[0]), pr) for pr in PV.get("props", [])]
fast = []
for name, ks, p in movers:
    for (ta, a), (tb, b) in zip(ks, ks[1:]):
        d, dt = math.dist(a[:2], b[:2]), tb - ta
        if d < 0.3 or dt <= 0 or (dt <= 0.15 and any(ta - 1e-6 <= c <= tb + 1e-6 for c in cut_ts)):
            continue
        lim = caps.get(name, 70.0 if p.get("vehicle") else (5.0 if _running(p, ta, tb) else 2.0))  # 载具按车速查（70 米/秒 ≈ 250 公里/小时）
        if d / dt > lim:
            fast.append(f"「{name}」{ta:.1f}–{tb:.1f} 秒走了 {d:.1f} 米（{d / dt:.1f} 米/秒，上限 {lim:g}）")
if fast:
    rows.append(({"id": "-", "name": "速度", "t0": 0, "t1": spec["duration"]}, "FAIL",
                 fast[:12] + ["人走得像快进：加长这一镜、缩短这段路，或者让跳位发生在切点上（换了时空）"]))

if len(set(axis_sides.values())) > 1:
    rows.append(({"id": "-", "name": "轴线", "t0": 0, "t1": spec["duration"]}, "WARN",
                 [f"机位在轴线两侧都出现：{axis_sides}（除非是有意换轴，并在规格里给该镜写 new_axis: true）"]))

# 身份（4.0 alpha.12，scenario-labs blockout skill）：模型按白模里每块东西的位置和大小生成，"像人的东西"会变成人
heights = []  # 机位高度统计（只统计，不拦）
ident = []
for sh in spec["shots"]:
    t = (sh["t0"] + sh["t1"]) / 2
    sc.frame_set(int(round(1 + t * FPS)))
    cam = cam_at(t)
    if cam is None:
        continue
    sc.camera = cam
    cp = cam.matrix_world.translation
    vis = lambda q: (lambda w: 0 <= w.x <= 1 and 0 <= w.y <= 1 and w.z > 0)(world_to_camera_view(sc, cam, q))  # noqa: E731
    roots = {n: bpy.data.objects[n].matrix_world.translation.copy() for n in people if n not in GIANTS}
    names = sorted(roots)
    for i, a_ in enumerate(names):  # 两个人叠在一起
        for b_ in names[i + 1:]:
            if (roots[a_] - roots[b_]).xy.length < 0.3 and vis(roots[a_] + Vector((0, 0, 1.0))):
                ident.append(f"镜 {sh['id']} {t:.1f}s「{a_}」和「{b_}」的身子叠在一起（相距 {(roots[a_] - roots[b_]).xy.length:.2f} 米），模型会画成一个人或多出肢体")
    for n in names:  # 头钻进墙里、家具里
        hd = bpy.data.objects.get(n + "_head")
        if hd is None or not vis(hd.matrix_world.translation):
            continue
        for o in boxes:
            if not o.name.startswith(("prop_", "fx_", "代理_")) and inside(o, hd.matrix_world.translation):
                ident.append(f"镜 {sh['id']} {t:.1f}s「{n}」的头在「{o.name}」里面，会被画成断头或穿墙")
                break
    for o in [x for x in bpy.data.objects if x.type == "MESH" and x.name.startswith("prop_")]:  # 人身边立着的零散方块
        if any(c.influence > 0.5 for c in o.constraints):  # 拿在手里、挂在身上的道具跟着人动，不算
            continue
        q = o.matrix_world.translation
        dims = o.dimensions
        if dims.z < 0.4 or max(dims.x, dims.y) > 1.2 or not vis(q):  # 小件、桌椅一类的大件都不算；只查半人高、细长的立着的块
            continue
        near = [n for n in names if (roots[n] - q).xy.length < 0.8]
        if near:
            ident.append(f"镜 {sh['id']} {t:.1f}s 道具「{o.name[5:]}」（{dims.z:.1f} 米高）立在「{near[0]}」身边 {(roots[near[0]] - q).xy.length:.1f} 米，容易被读成多出来的人：挪开一点，或在提示词里写明它是什么")
    main, _fg, _partial = subjects(sh["body"])
    for n in main:
        hd = bpy.data.objects.get(n + "_head")
        if hd is None or n in GIANTS:
            continue
        dz = cp.z - hd.matrix_world.translation.z
        if abs(dz) > 0.5:
            heights.append((sh, n, dz))
        break
if ident:
    rows.append(({"id": "-", "name": "身份", "t0": 0, "t1": spec["duration"]}, "WARN", sorted(set(ident), key=ident.index)[:12]))

lines = [f"# 白模几何检查：第 {spec['clip']} 条 {spec['title']}", "",
         "| 镜头 | 时间 | 结果 | 说明 |", "|---|---|---|---|"]
for sh, level, notes in rows:
    lines.append(f"| {sh['id']} {sh['name']} | {sh['t0']:g}–{sh['t1']:g} 秒 | {level} | {'；'.join(notes) or '主体入画、大小合适、没有被挡、头后干净'} |")
if comp:
    lines += ["", "## 构图（只统计，不拦）", "", "| 镜头 | 主体 | 头部水平位置 | 判断 |", "|---|---|---|---|"]
    for sh, p, x, eye, mid in comp:
        tag = "居中平视" if (eye and mid) else ("居中" if mid else ("黄金分割附近" if 0.32 <= x <= 0.44 or 0.56 <= x <= 0.68 else "偏边"))
        lines.append(f"| {sh['id']} {sh['name']} | {p} | {x:.0%} | {tag} |")
if side:
    lines += ["", "## 两人同框侧拍（只统计，不拦；docs/14 第三节 2.5）", "",
              "中景以上两人同框，机位接近两人连线的正侧面：画面没有纵深，打戏像横版游戏。对峙、打斗建议转到 30–60° 斜角；文戏有意为之可以不改。", "",
              "| 镜头 | 两人 | 视线和连线夹角 |", "|---|---|---|"]
    for sh, a_, b_, ang in side:
        lines.append(f"| {sh['id']} {sh['name']} | {a_}、{b_} | {ang:.0f}° |")
if heights:
    lines += ["", "## 机位高度（只统计，不拦）", "",
              "不是人眼高度的机位是一种表态：高机位往下压 = 渺小、被审视；低机位往上 = 威严、压迫。分镜里要说得出为什么这样拍，说不出就回到平视。", "",
              "| 镜头 | 主体 | 机位比眼睛 |", "|---|---|---|"]
    for sh, n, dz in heights:
        lines.append(f"| {sh['id']} {sh['name']} | {n} | {'高' if dz > 0 else '低'} {abs(dz):.1f} 米（{'俯拍' if dz > 0 else '仰拍'}） |")
open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
