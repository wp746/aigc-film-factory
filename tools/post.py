#!/usr/bin/env python3
"""后期（docs/21 第五节、docs/14 4.2 修片阶梯）：按条号把出好的片段拼成一条，修模型的固定毛病，统一调色，按分镜数据转场，混音。

  python3 tools/post.py projects/<项目> [--src 出片] [--head 1] [--tail 0] [--hold 0]
                        [--audio keep|mute|<音频文件>] [--ambience] [--bed-db -20] [--duck-db -8]
                        [--grade auto|off] [--strength 0.8] [--transitions auto|cut] [--lufs -14]
                        [--size 1920x1080] [--out 文件]
  python3 tools/post.py --selftest      用合成测试片自测（调色、去冻结帧、转场时长、响度）

片段放在 <项目>/出片/（--src 改目录），文件名以"第N条"开头，如 第1条.mp4、第2.1条_第2抽.mp4。
同一条有多个文件时，优先用名字里带"选"的，否则用最新的。每个场景的调色基准默认是这个场景的第一条，文件名带"基准"的优先。

一、每条先修模型的毛病：
  --head N   剪掉开头 N 帧（Seedance 2.5 第一帧过锐，默认 1）
  --tail N   剪掉结尾 N 帧（2.5 最后几帧会突然跳成尾帧的样子）
  --hold N   剪掉结尾后用最后一张好帧补回 N 帧，时长不变（对白、卡点不错位）
  按 BT.601 解码、BT.709 输出（Seedance 的 mp4 按 601 解码颜色才对）；lanczos 放大到 --size 并轻微锐化；24 帧。
二、调色匹配（--grade auto）：同一场景的每一条向基准条的颜色靠（亮度、反差、色温、饱和度），
  限幅：亮度最多约半档，色温、饱和度有上限，肤色只做三成；条内越拍越暗的按时间补回来。
  分镜里这一条写 "grade": "keep" 就不动；写 "light_change": "原因"（剧情里光本来就在变）就不补条内变化。
三、转场（--transitions auto），从 分镜.json 推，某一条写 "transition": "cut|black|dissolve"（可加 "transition_sec"）改掉进这一条的切点：
  同一场连续的戏 → 硬切，并去掉下一条开头的冻结帧；换场景 → 硬切，环境底提前 0.3 秒进来；
  换了世界（如午后 → 黎明）→ 淡入黑再淡出 0.5 秒；冷开场之后 → 留 0.3 秒黑；片尾 → 画面淡出 1 秒、声音 1.5 秒。
四、声音：--audio keep 保留片段自带的声音（默认），mute 静音，给一个音频文件就整轨替换（2.5 常混进不要的配乐）。
  --ambience 按场景铺环境底（交付/声音/环境_<场景>.wav），同一场景一整段不断，换场时交叉；有台词处再压 --duck-db。
  最后按 --lufs 统一响度（默认 -14 LUFS，峰值 -1 dB；写 0 不做）。
输出 交付/成片/<片名>_粗剪.mp4；logs/粗剪记录.md（每条校正量、每个切点的做法和色差、响度）；logs/切点对照.jpg（每个切点前后一帧，调色前 / 后）。旧文件不覆盖。
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FPS = 24
J_SEC = 0.3        # 换场时环境底提前进来的秒数
BLACK_SEC = 0.5    # 淡入黑再淡出的总时长
GAP_SEC = 0.3      # 冷开场之后的黑场
END_V, END_A = 1.0, 1.5
MICRO = 0.03       # 每条声音头尾的防爆音淡入淡出
LIM = {"L": 8.0, "ab": 4.0, "k": (0.85, 1.18), "c": (0.85, 1.15), "gamma": (0.8, 1.25)}


# ---------- 基础 ----------
def run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"{what}失败：{r.stderr[:1500]}")
    return r


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=nb_read_packets,r_frame_rate:format=duration", "-of", "json", path], capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    num, den = (st.get("r_frame_rate") or "24/1").split("/")
    has_a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
                           capture_output=True, text=True).stdout.strip() != ""
    return float(d.get("format", {}).get("duration", 0)), int(st.get("nb_read_packets", 0)), float(num) / float(den), has_a


def pick(src):
    by = {}
    for f in sorted(os.listdir(src)):
        m = re.match(r"第([\d.]+)条", f)
        if m and f.lower().endswith((".mp4", ".mov", ".mkv")):
            by.setdefault(m.group(1), []).append(os.path.join(src, f))
    out = []
    for n, fs in by.items():
        sel = [f for f in fs if "选" in os.path.basename(f)]
        out.append((n, sel[0] if sel else max(fs, key=os.path.getmtime)))
    return sorted(out, key=lambda x: [int(v) for v in x[0].split(".")])


def uniq(path):
    base, ext = os.path.splitext(path)
    k, p = 1, path
    while os.path.exists(p):
        k += 1
        p = f"{base}_{k}{ext}"
    return p


# ---------- 颜色 ----------
def _np():
    import numpy as np
    return np


def srgb2lab(rgb):
    np = _np()
    c = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = np.einsum('...j,ij->...i', c, M) / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def lab2srgb(lab):
    np = _np()
    fy = (lab[..., 0] + 16) / 116
    fx, fz = fy + lab[..., 1] / 500, fy - lab[..., 2] / 200
    f = np.stack([fx, fy, fz], -1)
    xyz = np.where(f ** 3 > 0.008856, f ** 3, (f - 16 / 116) / 7.787) * np.array([0.95047, 1.0, 1.08883])
    Mi = np.array([[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]])
    c = np.clip(np.einsum('...j,ij->...i', xyz, Mi), 0, 1)
    return np.clip(np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055), 0, 1)


def grab(path, t, tmp, tag, w=192):
    """抽一帧小图（按 709 解码，和调色时一致）；返回 Lab 数组。"""
    from PIL import Image
    np = _np()
    out = os.path.join(tmp, f"_f_{tag}.png")
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0, t):.4f}", "-i", path, "-frames:v", "1",
         "-vf", f"scale={w}:-2:in_color_matrix=bt709:in_range=tv", out], "抽帧")
    return srgb2lab(np.asarray(Image.open(out).convert("RGB"), dtype=float) / 255.0)


def head_frames(path, n, tmp, tag):
    from PIL import Image
    np = _np()
    pat = os.path.join(tmp, f"_h_{tag}_%02d.png")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-frames:v", str(n), "-vf", "scale=96:-2,format=gray", pat], "抽头帧")
    fs = sorted(f for f in os.listdir(tmp) if f.startswith(f"_h_{tag}_"))
    arr = [np.asarray(Image.open(os.path.join(tmp, f)), dtype=float) for f in fs]
    for f in fs:
        os.remove(os.path.join(tmp, f))
    return arr


def stats(lab):
    np = _np()
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    return {"L": float(L.mean()), "sd": float(L.std()), "a": float(a.mean()), "b": float(b.mean()),
            "C": float(np.hypot(a, b).mean())}


def mean_stats(labs):
    ss = [stats(x) for x in labs]
    return {k: sum(s[k] for s in ss) / len(ss) for k in ss[0]}


def de(s1, s2):
    return math.sqrt((s1["L"] - s2["L"]) ** 2 + (s1["a"] - s2["a"]) ** 2 + (s1["b"] - s2["b"]) ** 2)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def plan_grade(src, ref, strength):
    """把 src 的均值往 ref 拉 strength 那么多，限幅后的校正量。"""
    g = {"dL": clamp((ref["L"] - src["L"]) * strength, -LIM["L"], LIM["L"]),
         "k": clamp((ref["sd"] / max(src["sd"], 1e-3)) ** strength, *LIM["k"]),
         "da": clamp((ref["a"] - src["a"]) * strength, -LIM["ab"], LIM["ab"]),
         "db": clamp((ref["b"] - src["b"]) * strength, -LIM["ab"], LIM["ab"]),
         "c": clamp((ref["C"] / max(src["C"], 1e-3)) ** strength, *LIM["c"]), "muL": src["L"]}
    g["clamped"] = any(abs(v) >= lim - 1e-6 for v, lim in ((g["dL"], LIM["L"]), (g["da"], LIM["ab"]), (g["db"], LIM["ab"])))
    return g


def apply_lab(lab, g):
    """校正本身（LUT 和预测共用一套算法）。"""
    np = _np()
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    hue = np.degrees(np.arctan2(b, a))
    C = np.hypot(a, b)
    skin = ((hue > 20) & (hue < 75) & (C > 6) & (C < 50) & (L > 20) & (L < 90)).astype(float)
    w = 1 - 0.7 * skin                                       # 肤色只做三成
    L2 = g["muL"] + g["dL"] + (L - g["muL"]) * (1 + (g["k"] - 1) * w)
    edge = np.clip(np.minimum(L, 100 - L) / 12, 0, 1)        # 纯黑纯白附近收回，不压死也不冲爆
    L2 = L + (L2 - L) * edge
    cs = 1 + (g["c"] - 1) * w
    return np.stack([np.clip(L2, 0, 100), (a + g["da"] * w) * cs, (b + g["db"] * w) * cs], -1)


def predict(lab, seg):
    """这一条调完色以后，这一帧大概是什么样（gamma 取条尾的值）。"""
    if seg.get("gamma", 1.0) != 1.0:
        lab = srgb2lab(lab2srgb(lab) ** (1 / seg["gamma"]))
    return apply_lab(lab, seg["g"]) if seg.get("g") else lab


def write_lut(g, path, n=33):
    np = _np()
    r = np.linspace(0, 1, n)
    B, G, R = np.meshgrid(r, r, r, indexing="ij")          # .cube：红变化最快
    rgb = np.stack([R, G, B], -1).reshape(-1, 3)
    out = lab2srgb(apply_lab(srgb2lab(rgb), g))
    with open(path, "w") as f:
        f.write(f"LUT_3D_SIZE {n}\n")
        for v in out:
            f.write(f"{v[0]:.5f} {v[1]:.5f} {v[2]:.5f}\n")


def luma(lab):
    """Lab 的 L → 近似视频亮度码值（0–1），给条内补光的 gamma 用。"""
    np = _np()
    Y = np.where(lab[..., 0] > 8, ((lab[..., 0] + 16) / 116) ** 3, lab[..., 0] / 903.3)
    return float(np.where(Y <= 0.018, 4.5 * Y, 1.099 * Y ** 0.45 - 0.099).mean())


# ---------- 分镜数据 ----------
def load_board(proj):
    bp = os.path.join(proj, "分镜.json")
    board = json.load(open(bp, encoding="utf-8")) if os.path.exists(bp) else {"clips": []}
    world = {}
    for d in (proj, os.path.dirname(proj)):
        sp = os.path.join(d, "设定.json")
        if os.path.exists(sp):
            world = {l["name"]: l.get("world") for l in json.load(open(sp, encoding="utf-8")).get("locations", [])}
            break
    return {str(c["n"]): c for c in board.get("clips", [])}, world


def cut_kind(prev, cur, world, mode):
    """进 cur 这一条的切点怎么做。返回 (做法, 秒数, 说明)。"""
    if not prev or not cur:
        return "cut", 0, "硬切（没有分镜数据）"
    if cur.get("transition"):
        k = cur["transition"]
        sec = float(cur.get("transition_sec", {"black": BLACK_SEC, "dissolve": 0.5}.get(k, 0)))
        return k, sec, f"分镜指定 {k}"
    if mode == "cut":
        return "cut", 0, "硬切（--transitions cut）"
    if prev.get("cold_open_of"):
        return "gap", GAP_SEC, f"冷开场之后留 {GAP_SEC} 秒黑"
    same = prev.get("location") == cur.get("location")
    if same and not cur.get("time_jump"):
        return "splice", 0, "同一场连续：硬切"
    w1, w2 = world.get(prev.get("location")), world.get(cur.get("location"))
    if w1 and w2 and w1 != w2:
        return "black", BLACK_SEC, f"换了世界（{w1} → {w2}）：淡入黑再淡出"
    if same:
        return "cut", 0, "同场跳时间：硬切"
    return "cut", 0, "换场景：硬切，环境底提前进来"


def line_windows(clip, actual):
    """台词在这一条里的时间段（按实际时长缩放）。"""
    shots = clip.get("shots", []) if clip else []
    plan = sum(s.get("dur", 0) for s in shots) or 1
    t, out = 0.0, []
    for s in shots:
        d = s.get("dur", 0)
        if s.get("line"):
            out.append((t * actual / plan, (t + d) * actual / plan))
        t += d
    return out


def duck_expr(wins, g_lin, ramp=0.2):
    if not wins:
        return "1"
    terms = [f"clip(min((t-{a - ramp:.3f})/{ramp},({b + ramp:.3f}-t)/{ramp}),0,1)" for a, b in wins]
    m = terms[0]
    for t_ in terms[1:]:
        m = f"max({m},{t_})"
    return f"1-{1 - g_lin:.4f}*{m}"


# ---------- 主流程 ----------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("project", nargs="?")
    ap.add_argument("--src", default="出片")
    ap.add_argument("--head", type=int, default=1)
    ap.add_argument("--tail", type=int, default=0)
    ap.add_argument("--hold", type=int, default=0)
    ap.add_argument("--audio", default="keep")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--out")
    ap.add_argument("--ambience", action="store_true")
    ap.add_argument("--bed-db", type=float, default=-20.0)
    ap.add_argument("--duck-db", type=float, default=-8.0)
    ap.add_argument("--grade", choices=["auto", "off"], default="auto")
    ap.add_argument("--strength", type=float, default=0.8)
    ap.add_argument("--transitions", choices=["auto", "cut"], default="auto")
    ap.add_argument("--lufs", type=float, default=-14.0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.project:
        ap.error("要给项目目录")
    proj = os.path.abspath(a.project)
    src = a.src if os.path.isabs(a.src) else os.path.join(proj, a.src)
    if not os.path.isdir(src):
        sys.exit(f"没有片段目录 {src}：把出好的片段按 第N条.mp4 放进去")
    items = pick(src)
    if not items:
        sys.exit(f"{src} 里没有以 第N条 开头的视频")
    W, H = a.size.split("x")
    clips, world = load_board(proj)
    tmp = os.path.join(proj, "logs", "_post_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    replace = a.audio not in ("keep", "mute")

    # 1. 每条修毛病，统一成 709 / 24 帧中间片
    segs = []
    for n, f in items:
        dur, frames, fps, has_a = probe(f)
        start = a.head / fps
        end = max(start + 0.5, dur - a.tail / fps)
        plan_d = sum(s.get("dur", 0) for s in (clips.get(n) or {}).get("shots", []))
        if plan_d and 0.05 < dur - plan_d < 1.05:  # 小数秒的条按整秒出片，末尾是补的静帧（compile_video.py，4.0 alpha.10）：剪回分镜时长
            end = min(end, plan_d)
        vf = (f"trim=start={start:.4f}:end={end:.4f},setpts=PTS-STARTPTS,fps={FPS},"
              f"scale={W}:{H}:flags=lanczos:in_color_matrix=bt601:out_color_matrix=bt709,unsharp=5:5:0.4,setsar=1")
        if a.hold:
            vf += f",tpad=stop_mode=clone:stop={a.hold}"
        seg = end - start + a.hold / FPS
        raw = os.path.join(tmp, f"raw_{n}.mkv")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", f]
        keep_a = a.audio == "keep" and has_a
        if keep_a:
            af = f"atrim=start={start:.4f}:end={end:.4f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={seg:.4f}"
            cmd += ["-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]", "-c:a", "pcm_s16le"]
        else:
            cmd += ["-vf", vf, "-an"]
        cmd += ["-c:v", "libx264", "-crf", "12", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", raw]
        run(cmd, f"第{n}条预处理")
        rd, rf, _, _ = probe(raw)
        segs.append({"n": n, "file": f, "raw": raw, "src_dur": dur, "frames": rf, "has_a": keep_a, "clip": clips.get(n)})

    # 2. 定转场、去冻结帧、测颜色
    np = _np()
    prev = None
    for s in segs:
        c = s["clip"] or {}
        s["loc"] = c.get("location") or f"_无分镜_{s['n']}"
        s["kind"], s["tsec"], s["tnote"] = ("first", 0, "片头") if prev is None else cut_kind(prev["clip"], s["clip"], world, a.transitions)
        # 同一场连续（下一条从上一条尾帧接出来）：切点两侧本来就是同一个画面
        s["chain"] = bool(prev and prev["clip"] and c and prev["loc"] == s["loc"] and not c.get("time_jump")
                          and not prev["clip"].get("cold_open_of"))
        s["drop"] = 0
        if s["chain"]:                                           # 去开头冻结帧（最多 4 帧）
            fr = head_frames(s["raw"], 6, tmp, s["n"])
            while s["drop"] < min(4, len(fr) - 1) and float(np.abs(fr[s["drop"]] - fr[s["drop"] + 1]).mean()) < 0.3:
                s["drop"] += 1
        T = s["frames"] / FPS
        s["first"] = grab(s["raw"], (s["drop"] + 0.5) / FPS, tmp, f"{s['n']}f")
        s["last"] = grab(s["raw"], (s["frames"] - 1.5) / FPS, tmp, f"{s['n']}l")
        s["avg"] = mean_stats([grab(s["raw"], (k + 0.5) / 6 * T, tmp, f"{s['n']}a{k}") for k in range(6)])
        s["single"] = len(c.get("shots", [])) == 1
        if s["single"]:
            s["head"], s["tail"] = grab(s["raw"], 0.15 * T, tmp, f"{s['n']}h"), grab(s["raw"], 0.85 * T, tmp, f"{s['n']}t")
        prev = s

    # 3. 调色：同一场连续的切点按两侧同一画面对齐；同场景不连续的只修整体超出的部分；换场景不动
    anchors = {}
    for s in segs:
        if s["loc"] not in anchors or "基准" in os.path.basename(s["file"]):
            anchors[s["loc"]] = s
    for i, s in enumerate(segs):
        s["lut"], s["gamma"], s["g"], s["gnote"] = None, 1.0, None, "不调"
        c = s["clip"] or {}
        if a.grade == "off" or c.get("grade") == "keep":
            s["gnote"] = "不调（" + ("--grade off" if a.grade == "off" else "分镜写了 grade: keep") + "）"
            continue
        notes = []
        if c.get("light_change"):
            notes.append(f"条内光变化保留（{c['light_change']}）")
        elif s["single"]:                                        # 只有单镜头的条才能可靠地看出越拍越暗
            Lh, Lt = stats(s["head"])["L"], stats(s["tail"])["L"]
            lh, lt = luma(s["head"]), luma(s["tail"])
            if abs(Lh - Lt) > 3 and 0.02 < min(lh, lt) and max(lh, lt) < 0.98:
                s["gamma"] = clamp(math.log(lt) / math.log(lh), *LIM["gamma"])
                notes.append(f"条内{'变暗' if Lt < Lh else '变亮'} {abs(Lh - Lt):.1f}，按时间补（gamma 到 {s['gamma']:.2f}）"
                             + ("，已到上限，建议重剪或换一抽" if s["gamma"] in LIM["gamma"] else ""))
        if s["chain"]:
            p = segs[i - 1]
            tgt, src_ = stats(predict(p["last"], p)), stats(s["first"])
            d = de(src_, tgt)
            if d > 1.0:
                s["g"] = plan_grade(src_, tgt, 1.0)
                notes.append(f"对齐第{p['n']}条尾帧（色差 {d:.1f}）：亮度 {s['g']['dL']:+.1f}，反差 ×{s['g']['k']:.2f}，红绿 {s['g']['da']:+.1f}，"
                             f"黄蓝 {s['g']['db']:+.1f}，饱和 ×{s['g']['c']:.2f}" + ("（有一项到了上限，差得太多，建议看一眼）" if s["g"]["clamped"] else ""))
            else:
                notes.append(f"和第{p['n']}条尾帧色差 {d:.1f}，不用调")
        elif anchors[s["loc"]] is not s:
            ref = anchors[s["loc"]]
            d = de(s["avg"], ref["avg"])
            if d > 4:                                            # 不同镜头本来就不一样：只修超出 4 的部分
                s["g"] = plan_grade(s["avg"], ref["avg"], a.strength * (1 - 4 / d))
                notes.append(f"整体向第{ref['n']}条靠（整体色差 {d:.1f}，只修超出 4 的部分）：亮度 {s['g']['dL']:+.1f}，"
                             f"红绿 {s['g']['da']:+.1f}，黄蓝 {s['g']['db']:+.1f}")
            else:
                notes.append(f"和第{ref['n']}条整体色差 {d:.1f}，不用调")
        else:
            notes.append("本场景基准")
        if s["g"]:
            s["lut"] = os.path.join(tmp, f"lut_{s['n']}.cube")
            write_lut(s["g"], s["lut"])
        s["gnote"] = "；".join(notes)

    # 4. 时间线
    t = 0.0
    timeline = []
    for i, s in enumerate(segs):
        dur = (s["frames"] - s["drop"]) / FPS
        k, d = s["kind"], s["tsec"]
        if k == "gap":
            timeline.append({"black": d, "start": t})
            t += d
        if k == "dissolve" and i:
            t -= d
        s["start"], s["dur"] = t, dur
        s["fin"] = d / 2 if k == "black" else 0
        s["fout"] = 0
        if i and k == "black":
            segs[i - 1]["fout"] = d / 2
        t += dur
    total = t
    segs[-1]["fout"] = max(segs[-1]["fout"], END_V)

    # 5. 一次合成：画面、声音
    inputs, fc, alabels = [], [], []
    nin = [0]

    def add(*args):
        inputs.extend(args)
        nin[0] += 1
        return nin[0] - 1
    for s in segs:
        add("-i", s["raw"])
    blacks = [b for b in timeline if "black" in b]
    for b in blacks:
        b["idx"] = add("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d={b['black']:.4f}")
    vl = []
    for i, s in enumerate(segs):
        ch = f"[{i}:v]trim=start_frame={s['drop']},setpts=PTS-STARTPTS"
        if s["gamma"] != 1.0:
            T = s["dur"]
            ch += f",eq=gamma='1+{s['gamma'] - 1:.4f}*clip((t-{0.15 * T:.3f})/{0.7 * T:.3f},0,1)':eval=frame"
        if s["lut"]:
            ch += (f",scale=in_color_matrix=bt709:in_range=tv,format=gbrp,lut3d=file='{s['lut']}',"
                   f"scale=out_color_matrix=bt709:out_range=tv")
        if s["fin"]:
            ch += f",fade=t=in:st=0:d={s['fin']:.3f}"
        if s["fout"]:
            ch += f",fade=t=out:st={s['dur'] - s['fout']:.3f}:d={s['fout']:.3f}"
        ch += f",format=yuv420p,settb=1/{FPS},setsar=1[v{i}]"
        fc.append(ch)
        vl.append(f"v{i}")
    for b in blacks:
        fc.append(f"[{b['idx']}:v]format=yuv420p,settb=1/{FPS},setsar=1[vb{b['idx']}]")
    # 按时间顺序串起来
    order = []
    for i, s in enumerate(segs):
        for b in blacks:
            if abs(b["start"] + b["black"] - s["start"]) < 1e-6 and s["kind"] == "gap":
                order.append(("b", b))
        order.append(("s", i))
    acc, acc_len, k = None, 0.0, 0
    for typ, x in order:
        lab = f"vb{x['idx']}" if typ == "b" else f"v{x}"
        ln = x["black"] if typ == "b" else segs[x]["dur"]
        if acc is None:
            acc, acc_len = lab, ln
            continue
        k += 1
        if typ == "s" and segs[x]["kind"] == "dissolve":
            d = segs[x]["tsec"]
            fc.append(f"[{acc}][{lab}]xfade=transition=fade:duration={d:.3f}:offset={acc_len - d:.4f}[c{k}]")
            acc_len += ln - d
        else:
            fc.append(f"[{acc}][{lab}]concat=n=2:v=1:a=0,settb=1/{FPS}[c{k}]")
            acc_len += ln
        acc = f"c{k}"
    fc.append(f"[{acc}]null[vout]")

    # 声音：每条自带声音按时间放好
    if a.audio == "keep":
        for i, s in enumerate(segs):
            if not s["has_a"]:
                continue
            fi = max(MICRO, s["fin"], s["tsec"] if s["kind"] == "dissolve" else 0)
            nxt = segs[i + 1] if i + 1 < len(segs) else None
            fo = max(MICRO, s["fout"] if s is not segs[-1] else 0, nxt["tsec"] if nxt and nxt["kind"] == "dissolve" else 0)
            ms = int(round(s["start"] * 1000))
            fc.append(f"[{i}:a]atrim=start={s['drop'] / FPS:.4f},asetpts=PTS-STARTPTS,afade=t=in:d={fi:.3f},"
                      f"afade=t=out:st={s['dur'] - fo:.3f}:d={fo:.3f},adelay={ms}|{ms}[a{i}]")
            alabels.append(f"a{i}")
    elif replace:
        idx = add("-i", a.audio)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo[arep]")
        alabels.append("arep")
    # 环境底：同一场景一整段，换场提前 J_SEC 交叉
    bednotes = []
    if a.ambience:
        runs = []
        for s in segs:
            if runs and runs[-1]["loc"] == s["loc"]:
                runs[-1]["end"] = s["start"] + s["dur"]
                runs[-1]["segs"].append(s)
            else:
                runs.append({"loc": s["loc"], "start": s["start"], "end": s["start"] + s["dur"], "segs": [s]})
        g_lin = 10 ** (a.duck_db / 20)
        for r_i, r in enumerate(runs):
            bed = os.path.join(proj, "交付", "声音", f"环境_{r['loc']}.wav")
            if not os.path.exists(bed):
                bednotes.append(f"{r['loc']}：没有 环境_{r['loc']}.wav，这一段不铺")
                continue
            j = J_SEC if r_i else 0
            st = max(0.0, r["start"] - j)
            ln = r["end"] - st
            wins = [(s["start"] + w0 - st, s["start"] + w1 - st) for s in r["segs"] for w0, w1 in line_windows(s["clip"], s["dur"])]
            idx = add("-stream_loop", "-1", "-i", bed)
            ms = int(round(st * 1000))
            fo = J_SEC if r_i < len(runs) - 1 else 0.05
            fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{ln:.4f},asetpts=PTS-STARTPTS,"
                      f"volume={a.bed_db}dB,volume='{duck_expr(wins, g_lin)}':eval=frame,"
                      f"afade=t=in:d={max(j, 0.05):.3f},afade=t=out:st={ln - fo:.3f}:d={fo:.3f},adelay={ms}|{ms}[bed{r_i}]")
            alabels.append(f"bed{r_i}")
            bednotes.append(f"{r['loc']}：{st:.2f}–{r['end']:.2f} 秒，{a.bed_db:.0f} dB，台词处再压 {a.duck_db:.0f} dB（{len(wins)} 段台词）"
                            + (f"，提前 {J_SEC} 秒进来" if j else ""))
    if alabels:
        mix = "".join(f"[{x}]" for x in alabels)
        fc.append(f"{mix}amix=inputs={len(alabels)}:normalize=0:duration=longest,atrim=0:{total:.4f},apad=whole_dur={total:.4f},"
                  f"afade=t=out:st={max(0, total - END_A):.3f}:d={END_A}[aout]")
    else:
        idx = add("-f", "lavfi", "-t", f"{total:.4f}", "-i", "anullsrc=r=48000:cl=stereo")
        fc.append(f"[{idx}:a]anull[aout]")
    script = os.path.join(tmp, "graph.txt")
    open(script, "w", encoding="utf-8").write(";\n".join(fc))
    mixed = os.path.join(tmp, "mixed.mkv")
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex_script", script, "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "pcm_s16le", "-t", f"{total:.4f}", mixed], "合成")

    # 6. 响度
    title = os.path.basename(proj)
    final = a.out or uniq(os.path.join(proj, "交付", "成片", f"{title}_粗剪.mp4"))
    os.makedirs(os.path.dirname(final), exist_ok=True)
    loud = measure_loudness(mixed) if a.lufs else None
    if loud and loud["input_i"] not in ("-inf", "inf") and float(loud["input_i"]) > -70:
        af = (f"loudnorm=I={a.lufs}:TP=-1:LRA=11:measured_I={loud['input_i']}:measured_TP={loud['input_tp']}:"
              f"measured_LRA={loud['input_lra']}:measured_thresh={loud['input_thresh']}:offset={loud['target_offset']}:linear=true,aresample=48000")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", mixed, "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", final], "响度")
        after = measure_loudness(final)
        lnote = f"{float(loud['input_i']):.1f} → {float(after['input_i']):.1f} LUFS，峰值 {float(after['input_tp']):.1f} dBTP"
    else:
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", mixed, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final], "封装")
        lnote = "不做（--lufs 0）" if not a.lufs else "没有声音"

    # 7. 切点对照与记录
    rows, cutlog = [], []
    for i in range(1, len(segs)):
        p, s = segs[i - 1], segs[i]
        b_prev = grab(p["raw"], (p["frames"] - 1.5) / FPS, tmp, f"bp{i}", 320)
        b_next = grab(s["raw"], (s["drop"] + 0.5) / FPS, tmp, f"bn{i}", 320)
        ov = s["tsec"] if s["kind"] == "dissolve" else 0
        ta = p["start"] + p["dur"] - max(p["fout"], ov) - 1.5 / FPS
        tb = s["start"] + max(s["fin"], ov) + 0.5 / FPS
        if ov:   # 叠化处两条是混在一起的，用调色算法预测两侧各自的样子
            a_prev, a_next = predict(b_prev, p), predict(b_next, s)
        else:
            a_prev, a_next = grab(final, ta, tmp, f"ap{i}", 320), grab(final, tb, tmp, f"an{i}", 320)
        d0, d1 = de(stats(b_prev), stats(b_next)), de(stats(a_prev), stats(a_next))
        same = s["chain"]
        rows.append((f"{p['n']}→{s['n']}", b_prev, b_next, a_prev, a_next, d0, d1))
        cutlog.append(f"| {p['n']} → {s['n']} | {s['tnote']}" + (f"，去掉 {s['drop']} 帧冻结" if s["drop"] else "")
                      + f" | {s['start']:.2f} 秒 | {d0:.1f} → {d1:.1f}{'' if same else '（不是同一画面，不比）'} |")
    sheet = os.path.join(proj, "logs", "切点对照.jpg")
    if rows:
        contact_sheet(rows, sheet)
    log = ["# 粗剪记录", "",
           f"参数：剪头 {a.head} 帧，剪尾 {a.tail} 帧，补尾 {a.hold} 帧，声音 {a.audio}，调色 {a.grade}（强度 {a.strength}），"
           f"转场 {a.transitions}，环境底 {'开' if a.ambience else '关'}，{a.size}，{FPS} 帧", "",
           "## 每条", "", "| 条 | 文件 | 原时长 | 成片时长 | 在成片中的起止 | 调色 |", "|---|---|---|---|---|---|"]
    for s in segs:
        log.append(f"| {s['n']} | {os.path.basename(s['file'])} | {s['src_dur']:.2f} 秒 | {s['dur']:.2f} 秒 | "
                   f"{s['start']:.2f}–{s['start'] + s['dur']:.2f} 秒 | {s['gnote']} |")
    log += ["", "## 切点", "", "色差是切点前后一帧的平均色差（ΔE）。只有同一场连续的切点两侧是同一个画面，才拿来比：3 以内基本看不出，6 以上明显跳。", "",
            "| 切点 | 做法 | 在成片中 | 色差 调色前 → 后 |", "|---|---|---|---|"] + cutlog
    log += ["", "## 声音", "", f"- 响度：{lnote}"] + [f"- 环境底 {x}" for x in bednotes]
    log += ["", f"成片：{os.path.relpath(final, proj)}，{probe(final)[0]:.2f} 秒（时间线 {total:.2f} 秒）。",
            f"切点对照：{os.path.relpath(sheet, proj) if rows else '只有一条，没有切点'}。上排调色前、下排调色后，每格左边是上一条最后一帧、右边是下一条第一帧。",
            "要人看的：切点对照里还跳的切点、校正到了上限的条、片尾淡出的位置；声音好不好听要人听。"]
    open(os.path.join(proj, "logs", "粗剪记录.md"), "w", encoding="utf-8").write("\n".join(log) + "\n")
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n".join(log))
    return {"final": final, "total": total, "segs": segs, "loud": lnote, "rows": [(r[0], r[5], r[6]) for r in rows]}


def measure_loudness(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    return json.loads(m[-1]) if m else None


def contact_sheet(rows, path):
    from PIL import Image, ImageDraw
    img = lambda lab: Image.fromarray((lab2srgb(lab) * 255).astype("uint8"))  # noqa: E731
    w, h = rows[0][1].shape[1], rows[0][1].shape[0]
    pad, lab_h = 8, 22
    W_ = 4 * w + 5 * pad + 2 * pad
    H_ = len(rows) * (h + lab_h + pad) + pad + lab_h
    sheet = Image.new("RGB", (W_, H_), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    font = None
    for fp in ("/System/Library/Fonts/STHeiti Medium.ttc", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "C:/Windows/Fonts/msyh.ttc"):
        try:
            from PIL import ImageFont
            font = ImageFont.truetype(fp, 15)
            break
        except Exception:
            continue
    if font is None:  # 没有中文字体时用英文标签
        from PIL import ImageFont
        font = ImageFont.load_default()
    d.text((pad, 3), "调色前：上一条尾 | 下一条头" if font.getbbox("调") else "before", fill=(200, 200, 200), font=font)
    d.text((pad * 4 + 2 * w, 3), "调色后：上一条尾 | 下一条头", fill=(200, 200, 200), font=font)
    y = lab_h + pad
    for name, bp, bn, ap_, an, d0, d1 in rows:
        d.text((pad, y + 2), f"第{name}条   色差 ΔE {d0:.1f} → {d1:.1f}", fill=(230, 230, 230), font=font)
        xs = [pad, pad * 2 + w, pad * 4 + 2 * w, pad * 5 + 3 * w]
        for x, lab in zip(xs, (bp, bn, ap_, an)):
            sheet.paste(img(lab), (x, y + lab_h))
        y += h + lab_h + pad
    sheet.save(path, quality=88)


# ---------- 自测 ----------
def selftest():
    """合成 4 条测试片：第 2 条接第 1 条（同场连续），偏暗偏暖、开头 3 帧冻结；第 3 条换世界，单镜头越拍越暗；
    第 4 条接第 3 条的暗尾帧、叠化进来。"""
    tmpd = tempfile.mkdtemp(prefix="post_selftest_")
    proj = os.path.join(tmpd, "测试片")
    os.makedirs(os.path.join(proj, "出片"))
    os.makedirs(os.path.join(proj, "交付", "声音"))
    json.dump({"locations": [{"name": "甲", "world": "午后"}, {"name": "乙", "world": "黎明"}]},
              open(os.path.join(proj, "设定.json"), "w", encoding="utf-8"), ensure_ascii=False)
    sh = lambda d=2, line=False: {"dur": d, **({"line": {"who": "甲", "text": "台词"}} if line else {})}  # noqa: E731
    json.dump({"clips": [{"n": "1", "location": "甲", "shots": [sh(), sh()]},
                         {"n": "2", "location": "甲", "shots": [sh(line=True), sh()]},
                         {"n": "3", "location": "乙", "shots": [sh(4)]},
                         {"n": "4", "location": "乙", "transition": "dissolve", "shots": [sh(), sh(line=True)]}]},
              open(os.path.join(proj, "分镜.json"), "w", encoding="utf-8"), ensure_ascii=False)
    src = {"1": ("testsrc2=s=640x360:r=24:d=8", 0, "null"),
           "2": ("testsrc2=s=640x360:r=24:d=8", 4, "eq=brightness=-0.07,colorbalance=rm=0.12:bm=-0.10,tpad=start_mode=clone:start=3"),
           "3": ("smptehdbars=s=640x360:r=24:d=8", 0, "eq=brightness='-0.15*t/4':eval=frame"),
           "4": ("smptehdbars=s=640x360:r=24:d=8", 4, "eq=brightness=-0.13")}
    for n, (gen, ss, vf) in src.items():
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", gen, "-f", "lavfi", "-i", f"sine=f={300 + 100 * int(n)}:d=4:sample_rate=48000",
             "-vf", f"trim=start={ss},setpts=PTS-STARTPTS,{vf},trim=end_frame=96", "-af", "volume=-18dB",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", os.path.join(proj, "出片", f"第{n}条.mp4")], "造测试片")
    for loc in ("甲", "乙"):
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=3:c=pink:a=0.3", "-ar", "48000",
             os.path.join(proj, "交付", "声音", f"环境_{loc}.wav")], "造环境底")
    r = main([proj, "--ambience", "--size", "640x360"])
    fails = []
    segs = {s["n"]: s for s in r["segs"]}
    if segs["2"]["drop"] < 2:
        fails.append(f"第 2 条开头冻结帧只去掉 {segs['2']['drop']} 帧（应 ≥ 2）")
    for cut in ("1→2", "3→4"):
        c_ = [x for x in r["rows"] if x[0] == cut][0]
        if not c_[2] < 2.5:
            fails.append(f"同场连续的切点 {cut} 色差没压到 2.5 以内：{c_[1]:.1f} → {c_[2]:.1f}")
    if segs["3"]["gamma"] <= 1.0:
        fails.append("第 3 条越拍越暗没有补")
    if segs["3"]["kind"] != "black" or segs["4"]["kind"] != "dissolve":
        fails.append(f"转场判断错：第 3 条 {segs['3']['kind']}，第 4 条 {segs['4']['kind']}")
    exp = sum(s["dur"] for s in r["segs"]) - segs["4"]["tsec"]
    real = probe(r["final"])[0]
    if abs(real - exp) > 2 / FPS + 0.03:
        fails.append(f"成片时长 {real:.2f} 秒，应为 {exp:.2f} 秒")
    m = re.search(r"→ (-?[\d.]+) LUFS", r["loud"])
    if not m or abs(float(m.group(1)) + 14) > 1.0:
        fails.append(f"响度没到 -14：{r['loud']}")
    shutil.rmtree(tmpd, ignore_errors=True)
    if fails:
        print("✗ post.py 自测失败：\n- " + "\n- ".join(fails))
        sys.exit(1)
    print("✓ post.py 自测通过：同场调色匹配、去冻结帧、条内补光、淡入黑、叠化时长、环境底与响度。")


if __name__ == "__main__":
    main()
