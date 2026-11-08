#!/usr/bin/env python3
"""项目脚本公共库：每个项目的 `预演规格/build_assets.py`（资产）和 `预演规格/gen_clips.py`（镜头）只写数据，
怎么写文档、怎么导出白模规格和镜头规格单，全部由这里统一完成。所以无论哪个智能体来跑，产物格式都一样。

用法见 docs/18-自动化流水线.md；项目模板在 templates/project/。

资产条目（ITEMS 里每一项）的字段：
  tag      @角色名 / @prop_道具 / @loc_场景 / @crowd_世界
  world    用哪个风格配方（项目 STYLE 字典的键）
  file     出图文件名        type  人物三视图 | 状态变体 | 道具 | 场景主图 | 场景宫格 | 人类视角机位 | 群像
  rec      推荐模型          deps  依赖的序号（如 "1、3"）   use  用在哪几条
  lock     中文描述锁（逐字进规格单）   en  英文开头   must  英文必须可读的要点
  check    出图后检查清单（列表）       duty  Seedance 引用语（不含 @图片N）

镜头条目（clipN() 返回的字典）的字段见 export() 的说明。
"""
import json
import math
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- 风格配方 ----------------


def recipes(mapping):
    """{"天庭": "R01", ...} → {"天庭": "<配方英文>"}；配方见 config/style_recipes.json。"""
    rec = {r["id"]: r["en"] for r in json.load(open(os.path.join(ROOT, "config", "style_recipes.json"), encoding="utf-8"))["配方"]}
    return {k: rec[v] for k, v in mapping.items()}


# ---------------- 资产版式（docs/16、docs/17） ----------------

SHEET = ("LAYOUT: One wide 16:9 image with three views against a seamless neutral mid-grey backdrop tinted toward the STYLE palette. On the left half, two equally "
         "sized full-body views standing upright, directly from the front and directly from the back, identical in scale, head to feet in frame. "
         "On the right half, one large head-and-shoulders portrait, strictly frontal. On the front full-body figure the face is a softly blurred, "
         "featureless skin-tone oval that keeps the head shape and hair; the large portrait is the single detailed face reference. "
         "Light the figures with a soft key light in the STYLE color temperature plus a thin rim light; every view stays fully sharp from head to toe. "
         "No text, labels, numbers or watermarks anywhere.")

GIANT_SHEET = ("LAYOUT: One wide 16:9 design sheet of a single colossal creature or machine against a seamless neutral mid-grey backdrop tinted toward the STYLE palette. "
               "On the left two thirds, two full-body views in its natural stance (on four legs if it has four legs), identical in scale and fully in frame: "
               "a three-quarter front view and a straight side view. On the right third, one large close-up of its most recognisable part (head, face plate or chest). "
               "No human figures. Light it with a soft key light in the STYLE color temperature plus a thin rim light; every view stays fully sharp. "
               "No text, labels, numbers or watermarks anywhere.")

CROWD_LAYOUT = ("LAYOUT: One wide 16:9 image of 14 full-body adults in two rows of 7 against a seamless pure white studio background "
                "with soft, even light and only soft contact shadows under the feet. All 14 are photographed from the same camera distance "
                "at the same scale, so their real heights show: within each row every person's feet stand on the same ground line, and taller "
                "people's heads are clearly higher than shorter people's heads, following the heights listed (from about 145 cm to 192 cm). "
                "Realistic adult anatomy and proportions: the head is about one seventh of the body height, natural leg length, "
                "no oversized heads, no stretched or squashed figures. Evenly spaced, not overlapping, every figure fully in frame from head "
                "to feet, facing the camera in relaxed, ordinary everyday poses, no heroic poses. Every person is a clearly different "
                "individual: different face, age, gender, height, body type, hairstyle and costume; each holds or wears one prop of their job. "
                "All are adults. The STYLE below applies only to the people (skin, costume materials, color grade); the backdrop stays pure white. "
                "No text, labels, numbers or watermarks anywhere.")


def crowd_item(world_name, roster, tag, file, deps, use, style_key, refs_en, world_en, film):
    """群像条目。roster：14 个 (中文一句, 英文一句含身高 'about 170 cm tall')。"""
    assert len(roster) == 14, "群像固定 14 人（两排各 7）"
    zh = "；".join(f"{i}. {z}" for i, (z, _) in enumerate(roster, 1))
    r1 = " ".join(f"{i}) {e};" for i, (_, e) in enumerate(roster[:7], 1))
    r2 = " ".join(f"{i}) {e};" for i, (_, e) in enumerate(roster[7:], 8))
    return dict(tag=tag, world=style_key, file=file, type="群像", rec="Image 2.5（APIMart 2K）", deps=deps, use=use,
                lock=f"{world_name}群演 14 人（第 1 排 1–7，第 2 排 8–14，从左到右）：{zh}。",
                en=f"Create a crowd-extras reference lineup for a {film}: {world_en}. {refs_en} Match their rendering, realism and costume era exactly, "
                   f"but none of the people in this lineup is a main character.\n\n{CROWD_LAYOUT}",
                must=f"ROW 1, left to right: {r1}\nROW 2, left to right: {r2}",
                check=["14 个人、两排", "高矮按名单拉开、头身比正常", "没有两个人长得一样", "没有人和主角撞脸", "没有文字"],
                duty=f"「{world_name}群演」：画面里的群众演员从这张图里取长相、服装和道具，每个人只出现一次、不重复；这张图里的人都不是主角；不继承白底和排队版式。")


def _block(lang, body):
    return f"```{lang}\n{body}\n```"


def write_asset_doc(project_root, title, items, style, style_note, order_note=""):
    """写 交付/01-资产提示词.md（格式与 tools/image_gen.py 兼容）。"""
    rows = ["| 序 | 标签 | 文件名（建议） | 类型 | 推荐模型（理由） | 先要出好的图 | 用在哪几条视频 |", "|---|---|---|---|---|---|---|"]
    secs, kinds = [], {}
    for n, it in enumerate(items, 1):
        kinds[it["type"]] = kinds.get(it["type"], 0) + 1
        rows.append(f"| {n} | {it['tag']} | {it['file']} | {it['type']} | {it['rec']} | {it['deps']} | {it['use']} |")
        locked = f"LOCKED 描述锁 (verbatim)：\n{it['lock']}"
        mid = f"\n\n{SHEET}" if it["type"] == "人物三视图" else (f"\n\n{GIANT_SHEET}" if it["type"] == "巨物设定图" else "")
        st = style[it["world"]]
        art = f"\n\n美术要求（必须体现）：{it['art']}" if it.get("art") else ""
        text = f"{it['en']}\n\n{locked}{art}{mid}\n\n{it['must']}\n\nSTYLE: {st}"
        banana = f"A {st}. {it['en']} {it['must']}" + (f" 美术要求：{it['art']}" if it.get("art") else "")
        parts = [f"### {n}｜{it['tag']}｜{it['type']}", "", f"- **用途**：{it['use']}", f"- **先上传**：{it['deps']}",
                 f"- **推荐模型**：{it['rec']}", "- **提示词**：", "", _block("text", text), "", _block("banana", banana),
                 "", "- **出图后检查**：" + "；".join(it["check"]) + "。",
                 f"- **Seedance 引用语**：`@图片N — {it['duty']}`", "", "---", ""]
        secs.append("\n".join(parts))
    count = "、".join(f"{k} {v} 张" for k, v in kinds.items())
    head = (f"# 《{title}》资产提示词\n\n"
            "> 本文件由流水线生成（设定.json 或 预演规格/build_assets.py），不要手改；改描述锁请改源数据后重跑 `python3 tools/pipeline.py run <项目目录>`。\n"
            f"> 共 {len(items)} 张：{count}。\n> **全片风格**：{style_note}。每张图的 STYLE 段逐字套用所属世界的配方。"
            "**通用规则**：任何图里都不能出现文字、数字、印章、标志。\n\n## 出图清单（按顺序出）\n\n"
            + "\n".join(rows) + (f"\n\n{order_note}" if order_note else "") + "\n\n---\n\n")
    os.makedirs(os.path.join(project_root, "交付"), exist_ok=True)
    open(os.path.join(project_root, "交付", "01-资产提示词.md"), "w", encoding="utf-8").write(head + "\n".join(secs))
    return len(items)


# ---------------- 白模几何（单位：米） ----------------

LENS_DEG = {18: "100°", 24: "84°", 28: "75°", 35: "63°", 50: "47°", 85: "29°", 135: "18°"}


def add(o, v):
    return [o[0] + v[0], o[1] + v[1], o[2] + v[2]]


def b(name, mn, mx, color, keys=None):
    d = {"name": name, "min": list(mn), "max": list(mx), "color": color}
    if keys:
        d["keys"] = keys
    return d


def bo(name, o, mn, mx, color, keys=None):
    return b(name, add(o, mn), add(o, mx), color, keys)


def appear(t, end=None):
    """在 t 秒出现（缩放从 0.01 到 1），end 秒消失。"""
    k = [{"t": 0, "scale": [0.01, 0.01, 0.01]}, {"t": t - 0.01, "scale": [0.01, 0.01, 0.01]}, {"t": t, "scale": [1, 1, 1]}]
    if end:
        k += [{"t": end - 0.01, "scale": [1, 1, 1]}, {"t": end, "scale": [0.01, 0.01, 0.01]}]
    return k


def face_to(a, c):
    return math.degrees(math.atan2(c[0] - a[0], -(c[1] - a[1])))


def person(name, color, h, pts):
    """pts: [(t, pos, face 角度 或 目标点, lean)]"""
    keys = []
    for t, pos, f, lean in pts:
        face = f if isinstance(f, (int, float)) else face_to(pos, f)
        keys.append({"t": t, "pos": list(pos), "face": face, "lean": lean})
    return {"name": name, "color": color, "height": h, "keys": keys}


def head(pos, h):
    return [pos[0], pos[1], pos[2] + 0.9 * h]


def cam(start, lens, pos, look, end_pos=None, end_t=None, handheld=0.0):
    keys = [{"t": start, "pos": list(pos)}]
    if end_pos:
        keys.append({"t": end_t, "pos": list(end_pos)})
    s = {"start": start, "lens": lens, "keys": keys, "look_at": look}
    if handheld:
        s["handheld"] = handheld
    return s


def ots(fg, fg_h, target, axis=None, back=0.7, lat=0.45, dz=0.15):
    """越过 fg 的肩膀看 target 的机位；axis=(A, B) 时选在轴线同一侧（全场一致）。
    瘦小的前景人物用 back≈0.9、lat≈0.28，否则 50mm 下前景会整个出画（《天劫外包》第 6 条教训）。"""
    dx, dy = target[0] - fg[0], target[1] - fg[1]
    L = math.hypot(dx, dy)
    if L < 1e-6:
        raise SystemExit("过肩镜头的前景人物和被拍的人站在同一个位置，算不出机位：把两人放到不同的站位（marks）")
    dx, dy = dx / L, dy / L
    best = None
    for s in (1, -1):
        p = [fg[0] - dx * back - dy * lat * s, fg[1] - dy * back + dx * lat * s, fg[2] + fg_h * 0.92 + dz]
        if axis:
            (ax, ay), (bx, by) = axis[0][:2], axis[1][:2]
            if (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax) > 0:
                return p
        best = best or p
    return best


# ---------------- 导出白模规格 + 镜头规格单 ----------------

def _kind(it):
    t = it["type"]
    if t in ("人物三视图", "状态变体"):
        return "character"
    if it["tag"].startswith("@prop"):
        return "prop"
    return {"人类视角机位": "human_view", "群像": "crowd", "巨物设定图": "giant"}.get(t, "scene")


def export(project_root, project, short, items, world_boxes, clips, texts, previs_colors, world_color=(0.14, 0.15, 0.2)):
    """每条 clip 字典字段：n dur title summary model tags count context extra_chars voices sfx constraint axis people
    shots(每镜 t0 t1 name lens cam body [allow_small allow_occlude new_axis]) [extra_boxes] fail risk splice。
    texts：THREE STYLE MUSIC VOICE(dict) LIP ANCHOR。"""
    here = os.path.join(project_root, "预演规格")
    os.makedirs(os.path.join(project_root, "交付", "规格单"), exist_ok=True)
    idx = {it["tag"]: it for it in items}
    out = []
    for c in clips:
        n = c["n"]
        miss = [t for t in c["tags"] if t not in idx]
        if miss:
            raise SystemExit(f"第{n}条引用了资产清单里没有的标签：{miss}")
        shots_prev, spec_shots = [], []
        for k, sh in enumerate(c["shots"], 1):
            shots_prev.append(dict(sh["cam"], start=sh["t0"]))
            s = {"t0": sh["t0"], "t1": sh["t1"], "id": k, "name": sh["name"], "body": sh["body"]}
            for key in ("allow_small", "new_axis", "allow_occlude", "allow_exit", "struct"):
                if sh.get(key):
                    s[key] = sh[key]
            spec_shots.append(s)
        ratio = texts.get("RATIO", "16:9")
        prev = {"name": f"{short}_第{n}条", "duration": c["dur"], "fps": 24, "resolution": [720, 1280] if ratio == "9:16" else [1280, 720], "world_color": list(world_color),
                "boxes": world_boxes + c.get("extra_boxes", []), "people": c["people"], "shots": shots_prev}
        if c.get("props_previs"):
            prev["props"] = c["props_previs"]
        if c.get("effects_previs"):
            prev["effects"] = c["effects_previs"]
        if c.get("light_previs"):
            prev["light"] = c["light_previs"]
        json.dump(prev, open(os.path.join(here, f"第{n}条.json"), "w", encoding="utf-8"), ensure_ascii=False)
        assets, geo, chars = [], [], []
        for t in c["tags"]:
            it = idx[t]
            kind = _kind(it)
            name = t.lstrip("@").replace("loc_", "").replace("prop_", "").replace("crowd_", "群演_")
            a = {"tag": t, "file": "参考图定版/" + it["file"], "kind": kind, "name": name, "duty": "@图片N — " + it["duty"]}
            for key in ("brief_en", "voice_en"):  # 英文简述：海螺 H3 官方格式的 subject_definitions 用
                if it.get(key):
                    a[key] = it[key]
            assets.append(a)
            if t.startswith("@loc_") and it["lock"] not in geo:  # 宫格和主图共用一条空间锁，只写一次
                geo.append(it["lock"])
                if it.get("geo_note") and it["geo_note"] not in geo:  # 光逻辑、锚点物、时代锁：进视频提示词
                    geo.append(it["geo_note"])
            if kind in ("character", "giant"):
                chars.append(it["lock"])
        spec = {"schema": "aigc-shot-spec/1", "project": project, "clip": n, "title": c["title"], "duration": c["dur"], "ratio": ratio,
                "resolution": "720p", "summary": c["summary"], "model_hint": c["model"],
                "previs": f"预演/previs_{short}_第{n}条.mp4", "previs_spec": f"../预演规格/第{n}条.json",
                "previs_colors": previs_colors + ("，肉色小方块=特写里的手" if any(s.get("struct", {}).get("hand") for s in c["shots"]) else "")
                + ("，浅米色小人=群演" if any(p["name"].startswith("群演") for p in c["people"]) else "")
                + ("，有尺寸的灰白小块=道具（只取位置和动作）" if c.get("props_previs") else "")
                + ("，跳动的小石块、飘落的小方片、张开的深色缝、伸长的长条=特效（碎石、纸屑、裂缝、巨物的臂，只取位置、方向和时机）" if c.get("effects_previs") else ""),
                "assets": assets, "count": c["count"], "context": c["context"], "two_worlds": texts["THREE"], "style": c.get("style_zh", texts["STYLE"]),
                "geo": geo, "chars": chars + c.get("extra_chars", []), "music": texts["MUSIC"], "sfx": c["sfx"],
                "voices": [texts["VOICE"][v] for v in c.get("voices", [])], "lip": texts["LIP"], "shots": spec_shots,
                "constraint": texts["ANCHOR"] + c["constraint"], "ban": "无", "long_take": "", "fail": c["fail"], "risk": c["risk"],
                "splice": c["splice"]}
        if "proxy_map" in c:  # 白模里每个代理是谁、白模长什么样（4.0 alpha.10，四家编译器共用，见 tools/compile/model_prompts.py previs_map）
            spec["proxies"] = c["proxy_map"]
            spec["previs_look"] = {"lit": bool(c.get("light_previs")), "extras": any(p["name"].startswith("群演") for p in c["people"]),
                                   "props": bool(c.get("props_previs")), "effects": bool(c.get("effects_previs")),
                                   "hands": any(s.get("struct", {}).get("hand") for s in c["shots"])}
        if c.get("axis"):
            spec["axis"] = c["axis"]
        if c.get("chorus"):
            spec["chorus"] = c["chorus"]
        if texts.get("VOICE_EN"):  # 只有声音的角色（kind: voice）没有资产图，海螺 H3 从这里取音色英文
            ve = {v: texts["VOICE_EN"][v] for v in c.get("voices", []) if v in texts["VOICE_EN"]}
            if ve:
                spec["voice_en"] = ve
        if c.get("audio"):
            spec["audio"] = c["audio"]
        for key in ("style_en", "summary_en", "splice_in"):
            if c.get(key):
                spec[key] = c[key]
        if texts.get("MUSIC_EN"):
            spec["music_en"] = texts["MUSIC_EN"]
        json.dump(spec, open(os.path.join(project_root, "交付", "规格单", f"第{n}条.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        out.append((n, c["dur"], len(spec_shots)))
        print(f"第{n}条 {c['dur']} 秒 {len(spec_shots)} 镜")
    return out
