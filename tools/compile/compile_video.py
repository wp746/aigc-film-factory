#!/usr/bin/env python3
"""镜头规格单 → 三家视频模型的投喂包（网页手动出片用）和 API 请求（脚本出片用）。

用法：
  python3 -I tools/compile/compile_video.py <规格单.json> --model seedance|hailuo|wan [--style lean|full] [--out <投喂包根目录>]
  python3 -I tools/compile/compile_video.py <规格单目录> --model all          # 整部片、三个模型一次出

每个投喂包是一个文件夹：
  01_xxx.png …     按上传顺序编号的参考图（复制件，网页上照编号顺序上传就不会错位）
  视频1_白模.mp4    白模预演（海螺、万相按切点截成 ≤15 秒的子段）
  提示词.txt        整段复制进提示词框
  界面参数.txt      模型、模式、时长、分辨率、比例、声音
  api_request.json  该家 API 的请求体（素材是本地路径占位，提交脚本 tools/video_submit.py 负责换成 URL 或 Base64）
  检查.txt          本模型的硬限制核对结果（时长、素材数、字数）

写法规则见 docs/13（三家各自的语法与限制）与 docs/14（规格单格式）。--style lean 是默认：
只保留素材职责、人数、轴线、时间轴、声音与约束，长描述锁交给参考图（Seedance 官方建议中文 ≤500 字，信息越多越容易被忽略）；
--style full 额外带上空间地图与角色描述锁。
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import model_prompts as MP  # noqa: E402

LIMITS = {  # docs/13 第一节，2026-11 官方
    "seedance": {"名称": "Seedance 2.5（即梦 / 火山方舟）", "dur": (4, 30), "images": 30, "videos": 10, "video_total": 30, "chars": 5000, "lean_target": 1500},
    "hailuo": {"名称": "海螺 H3 / MiniMax（全能参考 Ref2VA）", "dur": (4, 15), "images": 9, "videos": 3, "video_total": 15, "chars": 7000, "lean_target": 4000,
               "words": (350, 500)},
    "wan": {"名称": "万相 3.0（参考生视频）", "dur": (2, 15), "images": 10, "videos": 5, "video_total": 15, "chars": 20000, "lean_target": 1500},
}
WAN_PREVIS = os.environ.get("WAN_PREVIS") == "1"  # 万相默认不喂白模（2026-11-09 用户决定）；production.json 写 "wan_previs": true 才喂
if not WAN_PREVIS:  # 不传参考视频时万相单条 2–30 秒（官方）；不喂白模就没有"参考视频 ≤ 15 秒"的限制
    LIMITS["wan"] = dict(LIMITS["wan"], 名称="万相 3.0（参考生视频，只传图）", dur=(2, 30), videos=0, video_total=0)
SPLIT_MAX = {"seedance": 30, "hailuo": 15, "wan": 15 if WAN_PREVIS else 30}
AUDIO_LIM = {"seedance": (10, 30, 0, 30), "hailuo": (3, 15, 0, 15), "wan": (5, 15, 0, 15)}  # 段数、总秒数、单段下限、单段上限（docs/13）
AUDIO_REF = {"seedance": "@音频{}", "hailuo": "<Audio {}>", "wan": "音频{}"}


def audio_secs(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def pick_audio(spec, model, shots):
    """音色参考：只取这一段里开口的人，按开口顺序取，超过该家上限的不放（检查.txt 里提醒）。"""
    n_max, tot_max, lo, hi = AUDIO_LIM[model]
    got, notes, tot = [], [], 0.0
    talk = "|".join(re.split(r"[；。]", seg)[-1] for sh in shots for seg in re.findall(r"([^{}]*)\{", sh["body"]))  # 每句台词前面那一小段：谁说
    for a in spec.get("audio", []):
        if a["name"] not in talk:
            continue
        src = os.path.join(spec["_dir"], a["file"])
        if not os.path.exists(src):
            notes.append(f"WAIT  还缺音色参考 {a['file']}（流水线 sound 阶段生成）")
            continue
        sec = audio_secs(src)
        if not lo <= sec <= hi:
            notes.append(f"WARN  {a['name']} 的音色参考 {sec:.1f} 秒，不在本模型要求的 {lo}–{hi} 秒，没有放进去")
        elif len(got) >= n_max or tot + sec > tot_max:
            notes.append(f"WARN  音色参考超过本模型上限（{n_max} 段、总 {tot_max} 秒），{a['name']} 的没有放进去，靠提示词里的音色描述")
        else:
            got.append(dict(a, src=src, secs=sec, ref=AUDIO_REF[model].format(len(got) + 1)))
            tot += sec
    return got, notes


def audio_duty(a):
    return f"{a['ref']} 是「{a['name']}」的音色参考：只取音色、音高、语调和口音，不取里面说的内容、语速和背景声；台词只说本条写的那几句。"


def fmt(x):
    return f"{x:g}"


# ---------- 规格单 ----------

def load_spec(path):
    s = json.load(open(path, encoding="utf-8"))
    s["_dir"] = os.path.dirname(os.path.dirname(os.path.abspath(path)))  # 规格单/ 的上一级 = 交付/
    return s


def split_by_cuts(spec, maxlen):
    """按硬切点把一条拆成若干个 ≤ maxlen 秒的子条；每个子条的时间轴从 0 开始。返回 [(t0, t1, shots)]。"""
    parts, cur, start = [], [], 0.0
    for sh in spec["shots"]:
        if cur and sh["t1"] - start > maxlen:
            parts.append((start, cur[-1]["t1"], cur))
            cur, start = [], sh["t0"]
        if sh["t1"] - sh["t0"] > maxlen:
            sys.exit(f"第 {spec['clip']} 条镜头 {sh['id']} 单镜 {sh['t1'] - sh['t0']:g} 秒，超过该模型单条上限 {maxlen} 秒：先在规格单里拆镜")
        cur.append(sh)
    if cur:
        parts.append((start, cur[-1]["t1"], cur))
    return parts


MIN_DUR = {"seedance": 4, "hailuo": 4, "wan": 2}
WAN_CNY = {"480P": 0.30, "720P": 0.60, "1080P": 1.20}  # 元/秒，第三方转述的官方标准价，未在百炼价格页核实（config/prices.json）


def no_previs(model):
    return model == "wan" and not WAN_PREVIS


def split_for(spec, model):
    """按模型拆条；尾段短于该模型下限时并进前一段（并后仍不超上限），并不了就把前一段的最后一镜让给尾段。"""
    parts = split_by_cuts(spec, SPLIT_MAX[model])
    lo, hi = MIN_DUR[model], SPLIT_MAX[model]
    if len(parts) > 1 and parts[-1][1] - parts[-1][0] < lo:
        (a0, a1, sa), (b0, b1, sb) = parts[-2], parts[-1]
        if b1 - a0 <= hi:
            parts[-2:] = [(a0, b1, sa + sb)]
        elif len(sa) > 1 and b1 - sa[-1]["t0"] <= hi and sa[-1]["t0"] - a0 >= lo:
            parts[-2:] = [(a0, sa[-1]["t0"], sa[:-1]), (sa[-1]["t0"], b1, [sa[-1]] + sb)]
    return parts


def prompt_for(spec, model, style, part):
    auds, _ = pick_audio(spec, model, part[2])
    sp = dict(spec, _audio=auds)
    return build_prompt(sp, model, style, part) if model == "seedance" else MP.BUILD[model](sp, part, short_duty)[0]


def fit_chars(spec, model, style, parts):
    """提示词超过这家的字数上限（海螺 H3 7000 字）时，在镜头之间再切一刀，两段都不短于该模型下限；切不开的照旧报 FAIL。"""
    cap, lo = LIMITS[model]["chars"], MIN_DUR[model]
    out, queue, n_cut = [], list(parts), 0
    while queue:
        part = queue.pop(0)
        t0, t1, shots = part
        if len(shots) < 2 or len(prompt_for(spec, model, style, part)) <= cap:
            out.append(part)
            continue
        cands = [k for k in range(1, len(shots)) if shots[k]["t0"] - t0 >= lo and t1 - shots[k]["t0"] >= lo]
        if not cands:
            out.append(part)
            continue
        k = min(cands, key=lambda k: abs(shots[k]["t0"] - (t0 + t1) / 2))
        queue[0:0] = [(t0, shots[k]["t0"], shots[:k]), (shots[k]["t0"], t1, shots[k:])]
        n_cut += 1
    note = f"提示词超过 {cap} 字，自动在镜头之间多切了 {n_cut} 刀，本条共 {len(out)} 段（按顺序出，后一段接前一段尾帧）" if n_cut else ""
    return out, note


def lines_of(body):
    return re.findall(r"\{([^}]*)\}", body)


# ---------- 各家的素材叫法 ----------

def ref_name(model, kind, i):
    if model == "seedance":
        return {"image": f"@图片{i}", "video": f"@视频{i}"}[kind]
    if model == "hailuo":
        return {"image": f"参考图{i}", "video": f"参考视频{i}"}[kind]
    return {"image": f"图{i}", "video": f"视频{i}"}[kind]


def short_duty(a, ref):
    """精简版素材职责：一句话说清只取什么。"""
    n = a["name"] or a["tag"].lstrip("@")
    if a["kind"] == "character":
        return f"{ref} 是「{n}」的外观（脸只取右侧大头像，正面全身像的模糊椭圆不是脸；不继承灰底和三视图版式）。"
    if a["kind"] == "human_view":
        return f"{ref} 是人间「{n}」镜头的机位、构图和画里的人与物。"
    if a["kind"] == "giant":
        return f"{ref} 是「{n}」的外形、材质、颜色和比例（不继承灰底和设定图版式）。"
    if a["kind"] == "crowd":
        return f"{ref} 是「{n}」的群众演员：画面里的路人从这张图里按编号取长相、服装和道具（第 1 排从左到右 1–7，第 2 排 8–14），每人只出现一次；这些人都不是主角；不继承白底和排队版式。"
    if a["kind"] == "first":
        return f"{ref} 是本条的第一帧：开场的构图、人物外观、场景和光以它为准，之后按时间轴动起来。"
    if a["kind"] == "tail":
        return f"{ref} 是上一条视频的最后一帧：只取人物的位置、朝向、姿态、服装和伤湿状态，以及光，让这一条接得上；不作本条首帧构图，不继承它的画质。"
    if a["kind"] == "prop":
        return f"{ref} 是「{n}」的外形与材质（不继承灰底与并排版式）。"
    return f"{ref} 是「{n}」的空间、材质和光（不作首帧，不继承构图）。"


def convert_refs(text, model, n_images):
    """把规格单里 Seedance 写法的 @图片N / @视频1 换成目标模型的叫法。"""
    text = re.sub(r"@图片(\d+)", lambda m: ref_name(model, "image", int(m.group(1))), text)
    return re.sub(r"@视频(\d+)", lambda m: ref_name(model, "video", int(m.group(1))), text)


def dialogue(text, model):
    """台词：Seedance 用 {台词}；其余三家用中文引号。"""
    if model == "seedance":
        return text
    return re.sub(r"\{([^}]*)\}", r"“\1”", text)


# ---------- 提示词 ----------

def shot_line(model, sh, t0, idx):
    a, b = sh["t0"] - t0, sh["t1"] - t0
    body = dialogue(sh["body"], model)
    if model == "seedance":
        return f"{fmt(a)}–{fmt(b)} 秒｜镜头{sh['id']} {sh['name']}（同预演 {fmt(a)}–{fmt(b)} 秒）：{body}"
    if model == "hailuo":
        return f"{fmt(a)}–{fmt(b)} 秒（同参考视频1 的 {fmt(a)}–{fmt(b)} 秒）｜{sh['name']}：{body}"
    return f"第{idx}个镜头[{fmt(a)}-{fmt(b)}秒]（同视频1 的 {fmt(a)}–{fmt(b)} 秒）｜{sh['name']}：{body}"


def previs_duty(model, spec, vref):
    desc, rows, _ = MP.previs_map(spec, MP.ref_lookup(spec["assets"], lambda i, a: ref_name(model, "image", i)))
    return (f"{vref} 是本条的白模预演，只提供镜头路线、切点、构图、空间布局、人物整体位置与路线、事件先后；"
            f"外形、识别色、方块和白模的光都不继承；小人的姿势只表示谁在什么时候做什么动作，动作本身按真人自然重新生成，不照搬僵硬的姿势、平移滑行和机械摆臂。{desc}。逐个对应：" + "；".join(rows) + "。")


def build_prompt(spec, model, style, part):
    t0, t1, shots = part
    imgs = [a for a in spec["assets"]]
    out = []
    head = {"seedance": "【素材职责】", "hailuo": "【参考素材】", "wan": "【参考素材】"}[model]
    out.append(head)
    for i, a in enumerate(imgs, 1):
        ref = ref_name(model, "image", i)
        if style == "full" and model == "seedance":
            out.append(a["duty"].replace("@图片N", ref))
        else:
            out.append(short_duty(a, ref))
    vref = ref_name(model, "video", 1)
    out.append(previs_duty(model, spec, vref))
    out += [audio_duty(a) for a in spec.get("_audio", [])]
    out.append("角色外貌、服饰、位置、台词互不交换；画面质感从第一帧开始就按下面的质感描述。")
    out += ["", convert_refs(spec["count"], model, len(imgs))]
    out += ["", "【场景上下文】" if model == "seedance" else "【总体设定】", spec["context"], spec["two_worlds"], spec["style"]]
    axis = [g for g in spec["geo"] if g.startswith("调度轴线")]
    if style == "full":
        out += ["", "【空间地图】"] + spec["geo"] + ["", "【角色】"] + spec["chars"]
    elif axis:
        out += ["", axis[0]]
    cuts = "、".join(fmt(sh["t0"] - t0) for sh in shots[1:])
    dur = t1 - t0
    out += ["", "【摄影规格】" if model == "seedance" else "【镜头】",
            (f"本条只在以下时间点硬切：{cuts} 秒（与预演视频一致），镜头不自行切换；同一个镜头号是同一个机位。每镜一个视场角、一个主运镜。"
             if cuts else "本条是一个连续镜头，中途不切。")]
    out += ["", "【时间轴】" if model in ("seedance", "hailuo", "wan") else "【事件顺序】"]
    for k, sh in enumerate(shots, 1):
        out.append(convert_refs(shot_line(model, sh, t0, k), model, len(imgs)))
    sound = [spec["music"], "现场声：" + spec["sfx"] + "。"]
    if spec["voices"]:
        sound.append("音色：" + "".join(spec["voices"]))
    lip = spec["lip"] if model == "seedance" else spec["lip"].replace("花括号", "引号")
    sound.append(lip)
    out += ["", "【声音】"] + sound + ["", "【约束】", spec["constraint"]]
    if dur != spec["duration"]:
        out.insert(0, f"（本段是第 {spec['clip']} 条的第 {fmt(t0)}–{fmt(t1)} 秒，单独生成，剪辑时按顺序接回去。）")
    return "\n".join(out)


# ---------- 投喂包 ----------

def cut_video(src, dst, a, b):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", fmt(a), "-i", src, "-t", fmt(b - a), "-c:v", "libx264", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-an", dst], check=True)


def api_request(spec, model, prompt, files, video, dur):
    if model == "seedance":
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": f"file://{f}"}, "role": "reference_image"} for f in files]
        content.append({"type": "video_url", "video_url": {"url": f"file://{video}"}, "role": "reference_video"})
        content += [{"type": "audio_url", "audio_url": {"url": f"file://{a['pack']}"}, "role": "reference_audio"} for a in spec.get("_audio", [])]
        return {"_endpoint": "POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
                "model": os.environ.get("SEEDANCE_API_MODEL", "doubao-seedance-2-5-260628"), "content": content, "resolution": spec["resolution"], "ratio": spec["ratio"],
                "duration": int(dur), "generate_audio": True, "watermark": False, "omni_reference_task_type": "reference"}
    if model == "hailuo":
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "url": {"url": f"file://{f}"}, "role": "reference_image"} for f in files]
        content.append({"type": "video_url", "url": {"url": f"file://{video}"}, "role": "reference_video"})
        return {"_endpoint": "POST https://api.minimax.io/v2/video_generation", "model": "MiniMax-H3", "content": content,
                "resolution": "2K", "duration": int(dur), "ratio": spec["ratio"]}
    if model == "wan":
        media = [{"type": "reference_image", "url": f"file://{f}"} for f in files] + ([{"type": "reference_video", "url": f"file://{video}"}] if video else [])
        return {"_endpoint": "POST https://{WorkspaceId}.{region}.maas.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
                "_headers": {"X-DashScope-Async": "enable"}, "model": "wan3.0-video", "input": {"prompt": prompt, "media": media},
                "parameters": {"resolution": spec["resolution"].upper(), "ratio": spec["ratio"], "duration": int(dur), "audio": True,
                               "prompt_extend": False, "watermark": False}}
    raise ValueError(f"未知模型：{model}")


def ui_params(spec, model, dur, n_img):
    lim = LIMITS[model]
    rows = [f"模型：{lim['名称']}", f"时长：{fmt(dur)} 秒（必须和白模一样长）", f"比例：{spec['ratio']}"]
    if model == "seedance":
        rows += ["模式：全能参考", f"分辨率：{spec['resolution']}", "声音：开", "不开自动改写",
                 f"上传：先按编号上传 {n_img} 张图（@图片1…），再上传 视频1_白模.mp4（@视频1）"]
    elif model == "hailuo":
        rows += ["模式：全能参考 Ref2VA（参考生视频）。不要同时传首尾帧：首尾帧模式不收参考视频，白模用不上",
                 "分辨率：官方网页 / API 选 2K（试错 768P）；本地部署只有 768P（2K 的 Regenerate 模型没开源）",
                 "提示词：整段粘贴，英文分段格式是 H3 官方 skill 的格式，台词保留中文在 <d>[Chinese] …</d> 里，不要翻译、不要改标签",
                 f"上传：按编号上传 {n_img} 张参考图（对应 <Subject 1…>），再上传 视频1_白模.mp4（<Video 1>）。参考视频 2–15 秒，素材合计 ≤ 12 个",
                 "本地部署（ComfyUI / diffusers，MiniMax-H3 Ref2VA 权重）：同一份提示词和素材，输出 768P、24fps"]
    elif no_previs(model):
        rows[1] = f"时长：{fmt(dur)} 秒"
        rows += ["模式：参考生视频（多模态参考），只传图、不传白模：镜头、景别、焦段、站位和姿势全写在提示词的分镜里",
                 "分辨率：720P（试错用 480P）", "智能扩写（prompt_extend）：关（开着会改写提示词，分镜和台词会被改）", "声音：开", "输出帧率 30fps",
                 f"上传：按编号上传 {n_img} 张图（图1…）",
                 f"花费：按输出 {int(dur)} 秒计，480P 约 ¥{WAN_CNY['480P'] * int(dur):.1f}，720P 约 ¥{WAN_CNY['720P'] * int(dur):.1f}（第三方转述的官方价，以百炼账单为准）"]
    else:
        rows += ["模式：参考生视频（多模态参考）。不要用首帧 / 首尾帧入口：那两个模式不收参考视频，白模用不上", "分辨率：720P（试错用 480P）",
                 "智能扩写（prompt_extend）：关（开着会改写提示词，分镜和台词会被改）", "声音：开", "输出帧率 30fps（白模 24fps 不影响）",
                 f"上传：按编号上传 {n_img} 张图（图1…），再上传 视频1_白模.mp4（视频1）。参考视频合计 ≤ 15 秒，输入 + 输出 ≤ 30 秒",
                 f"花费：白模秒数也计费，按 {int(dur) * 2} 秒计，480P 约 ¥{WAN_CNY['480P'] * int(dur) * 2:.1f}，720P 约 ¥{WAN_CNY['720P'] * int(dur) * 2:.1f}（以百炼账单为准）"]
    return "\n".join(rows) + "\n"


def audio_upload(spec, model):
    auds = spec.get("_audio", [])
    if not auds:
        return []
    names = "、".join(f"音频{i}_{a['name']}.wav（{a['ref']}）" for i, a in enumerate(auds, 1))
    tip = {"hailuo": "参考音频要和图、视频一起传", "wan": "参考音频合计 ≤ 15 秒",
           "seedance": "参考音频合计 ≤ 30 秒"}[model]
    return [f"音色参考：最后上传 {names}；{tip}。只取音色，不取内容"]


ASSET_RANK = {"character": 0, "giant": 0, "scene": 1, "crowd": 1, "first": 2, "tail": 2, "human_view": 3, "prop": 3}


def fit_assets(assets, cap):
    """参考图超过这家的上限时，先丢多视角宫格、道具，再丢其他；人物和巨物最后丢。返回 (留下的, 丢掉的)，留下的保持原顺序。"""
    if len(assets) <= cap:
        return assets, []
    rank = lambda a: 3.5 if str(a.get("tag", "")).endswith("_grid") else ASSET_RANK.get(a["kind"], 3)  # noqa: E731
    order = sorted(range(len(assets)), key=lambda i: (rank(assets[i]), i))
    keep = sorted(order[:cap])
    return [assets[i] for i in keep], [assets[i] for i in sorted(order[cap:])]


def pad_video(path, secs):
    """白模末尾补静帧到整数秒（三家的时长都是整秒）。"""
    tmp = path + ".pad.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"tpad=stop_mode=clone:stop_duration={secs:.3f}", "-c:v", "libx264",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-an", tmp], check=True)
    os.replace(tmp, path)


def hold_line(model, dur, gen):
    pad = gen - dur
    if model == "hailuo":
        return (f"From {MP.mmss(dur)} to {MP.mmss(gen)}, the last shot holds its final framing: the people only breathe and blink, "
                "and the camera stays still (this tail is trimmed in editing).")
    return f"{fmt(dur)}–{gen} 秒：保持上一镜结束时的画面，人物只有呼吸和眨眼，镜头不动（这 {fmt(pad)} 秒剪辑时剪掉）。"


def compile_one(spec, model, style, out_root):
    lim = LIMITS[model]
    base = spec["_dir"]
    tail_note = []
    sp = spec.get("splice_in")
    if sp:  # 连续的戏：上一条的尾帧作为参考图接进来（pipeline.py splice 截取）
        if os.path.exists(os.path.join(base, sp["file"])) and len(spec["assets"]) < lim["images"]:
            spec = dict(spec, assets=spec["assets"] + [{"tag": "@tail", "file": sp["file"], "kind": "tail", "name": f"第{sp['from']}条尾帧",
                                                         "duty": f"@图片N — 第{sp['from']}条视频的最后一帧：只取人物的位置、朝向、姿态、服装和伤湿状态，以及光，让这一条接得上；不作本条首帧构图，不继承它的画质。"}])
        elif not os.path.exists(os.path.join(base, sp["file"])):
            tail_note.append(f"INFO  本条接第{sp['from']}条：那条出片后跑 `python3 tools/pipeline.py splice <项目> 第{sp['from']}条 <视频>` 截尾帧，再 run 重编，尾帧会作为参考图接进来")
        else:
            tail_note.append(f"WARN  参考图已满 {lim['images']} 张，第{sp['from']}条尾帧没接进来")
    ff = next((f"首帧图/第{spec['clip']}条{e}" for e in (".png", ".jpg") if os.path.exists(os.path.join(base, f"首帧图/第{spec['clip']}条{e}"))), None)
    if ff:  # 用户点头出过、目检过的首帧图（docs/14 第五节）：只给本条第一段，放在最后，不打乱原来的图号
        if len(spec["assets"]) < lim["images"]:
            spec = dict(spec, assets=spec["assets"] + [{"tag": "@first", "file": ff, "kind": "first", "name": f"第{spec['clip']}条首帧",
                                                         "duty": "@图片N — 本条的第一帧：开场的构图、人物外观、场景和光以它为准，之后按时间轴动起来。"}])
        else:
            tail_note.append(f"WARN  参考图已满 {lim['images']} 张，首帧图没接进来")
    previs = os.path.join(base, spec["previs"])
    parts, resplit = fit_chars(spec, model, style, split_for(spec, model))
    if resplit:
        tail_note.append(f"INFO  {resplit}")
    made = []
    whole = spec
    for k, part in enumerate(parts, 1):
        t0, t1, shots = part
        spec = whole if k == 1 else dict(whole, assets=[a for a in whole["assets"] if a["kind"] != "first"])
        fitted, dropped = fit_assets(spec["assets"], lim["images"])
        spec = dict(spec, assets=fitted)  # 提示词里的图号和真正上传的图一致（以前多出来的图被截掉，提示词还在引用）
        name = f"第{spec['clip']}条" + (f"_{k}" if len(parts) > 1 else "")
        d = os.path.join(out_root, model, name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        assets = spec["assets"]
        files = []
        for i, a in enumerate(assets, 1):
            src = os.path.join(base, a["file"])
            dst = os.path.join(d, f"{i:02d}_{os.path.basename(a['file'])}")
            if os.path.exists(src):
                shutil.copy2(src, dst)
            else:  # 图还没出：先占位，出完图重编一次即可
                open(dst + ".缺图.txt", "w", encoding="utf-8").write(f"等待定稿图：{a['file']}\n")
            files.append(dst)
        auds, anotes = pick_audio(spec, model, shots)
        for k2, a in enumerate(auds, 1):
            dst = os.path.join(d, f"音频{k2}_{a['name']}.wav")
            shutil.copy2(a["src"], dst)
            a["pack"] = dst
        spec = dict(spec, _audio=auds)
        video = os.path.join(d, "视频1_白模.mp4")
        if no_previs(model):  # 万相默认只传图：镜头、站位、姿势全写在分镜里
            video = None
        elif not os.path.exists(previs):
            open(video + ".缺白模.txt", "w", encoding="utf-8").write(f"等待白模：{spec['previs']}\n")
        elif len(parts) == 1:
            shutil.copy2(previs, video)
        else:
            cut_video(previs, video, t0, t1)
        dur = round(t1 - t0, 3)
        gen = max(math.ceil(dur - 1e-6), LIMITS[model]["dur"][0])  # 三家的时长都按整秒：小数秒的条向上补齐，白模末尾补静帧
        if gen > dur and video and os.path.exists(video):
            pad_video(video, gen - dur)
        notes = []
        if dropped:
            gone = [a["name"] for a in dropped]
            lost = [a["name"] for a in dropped if a["kind"] in ("character", "giant")]
            notes.append(("FAIL" if lost else "WARN") + f"  参考图超过 {lim['images']} 张上限，没放进来：{'、'.join(gone)}"
                         + (f"；其中人物 {'、'.join(lost)} 在白模里有代理却没有参考图，会被画成纯色小人：拆条、减人，或换参考图多的模型" if lost else ""))
        if model == "seedance":
            prompt = build_prompt(spec, model, style, part)
        else:
            prompt, fallback = MP.BUILD[model](spec, part, short_duty)
            if fallback:
                notes.append(f"WARN  镜头 {fallback} 没有结构化数据（项目不是 设定.json + 分镜.json 写的），这几镜退回中文正文，不是{lim['名称']}官方格式；改用 docs/19 的数据格式可得到完整官方格式")
        if gen > dur:  # 补的静帧写进时间轴的最后（各家时间轴后面那一段的开头作锚点）
            mark = {"seedance": "\n\n【声音】", "hailuo": "\n\noverall_soundscape", "wan": "\n\n台词"}[model]
            i = prompt.find(mark)
            prompt = (prompt[:i] + "\n" + hold_line(model, dur, gen) + prompt[i:]) if i >= 0 else prompt + "\n" + hold_line(model, dur, gen)
            notes.append(f"INFO  本段 {fmt(dur)} 秒，模型只收整秒：按 {gen} 秒出，白模末尾补了 {fmt(gen - dur)} 秒静帧；后期粗剪按分镜时长自动剪掉")
        missing_proxy = [] if no_previs(model) else MP.previs_map(spec, MP.ref_lookup(spec["assets"], lambda i, a: f"图{i}"))[2]
        if missing_proxy:
            notes.append(f"FAIL  白模里的 {'、'.join(missing_proxy)} 没有对应的参考图，模型会把代理原样画出来")
        open(os.path.join(d, "提示词.txt"), "w", encoding="utf-8").write(prompt + "\n")
        open(os.path.join(d, "界面参数.txt"), "w", encoding="utf-8").write(ui_params(spec, model, gen, len(files)) + "".join(x + "\n" for x in audio_upload(spec, model)))
        json.dump(api_request(spec, model, prompt, files, video, gen), open(os.path.join(d, "api_request.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        chk = check_limits(spec, model, prompt, gen, len(spec["assets"]), style) + notes + anotes + (tail_note if k == 1 else [])
        if auds:
            chk.append(f"PASS  音色参考 {len(auds)} 段，共 {sum(a['secs'] for a in auds):.1f} 秒（{'、'.join(a['name'] for a in auds)}）")
        missing = [a["file"] for a in assets if not os.path.exists(os.path.join(base, a["file"]))]
        if missing:
            chk.append(f"WAIT  还缺 {len(missing)} 张定稿图：{'、'.join(os.path.basename(m) for m in missing)}；出完图重编一次")
        open(os.path.join(d, "检查.txt"), "w", encoding="utf-8").write("\n".join(chk) + "\n")
        made.append((d, len(prompt), chk))
    return made


def check_limits(spec, model, prompt, dur, n_assets, style):
    lim = LIMITS[model]
    out = []
    lo, hi = lim["dur"]
    out.append(("PASS" if lo <= dur <= hi else "FAIL") + f"  时长 {fmt(dur)} 秒（{lim['名称']} 允许 {lo}–{hi} 秒）")
    out.append(("PASS" if n_assets <= lim["images"] else "FAIL") + f"  参考图 {n_assets} 张（上限 {lim['images']}）"
               + ("：超出的图没有放进投喂包，先在规格单里减图" if n_assets > lim["images"] else ""))
    n = len(prompt)
    out.append(("PASS" if n <= lim["chars"] else "FAIL") + f"  提示词 {n} 字（上限 {lim['chars']}）")
    if lim.get("words"):
        w = MP.english_words(prompt)
        lo_w, hi_w = lim["words"]
        out.append(("PASS" if w <= hi_w * 1.5 else "WARN") + f"  英文 {w} 词（H3 官方：生成任务通常 {lo_w}–{hi_w} 词，台词多时以台词为先）")
    if style == "lean" and n > lim["lean_target"]:
        out.append(f"WARN  精简版仍有 {n} 字，超过建议的 {lim['lean_target']} 字：信息越多越容易被模型忽略，考虑压镜头描述")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", help="规格单 .json，或整个规格单目录")
    ap.add_argument("--model", required=True, choices=list(LIMITS) + ["all"])
    ap.add_argument("--style", default="lean", choices=["lean", "full"])
    ap.add_argument("--out", default=None, help="投喂包根目录，默认 交付/投喂包")
    a = ap.parse_args()
    paths = sorted(os.path.join(a.spec, f) for f in os.listdir(a.spec) if f.endswith(".json")) if os.path.isdir(a.spec) else [a.spec]
    models = list(LIMITS) if a.model == "all" else [a.model]
    for p in paths:
        spec = load_spec(p)
        out_root = a.out or os.path.join(spec["_dir"], "投喂包")
        for m in models:
            for d, n, chk in compile_one(spec, m, a.style, out_root):
                bad = [c for c in chk if c.startswith("FAIL")]
                print(f"{'FAIL' if bad else 'OK  '} {os.path.relpath(d, spec['_dir'])}  {n} 字" + ("  " + "；".join(bad) if bad else ""))


if __name__ == "__main__":
    main()
