#!/usr/bin/env python3
"""海螺 H3 / 万相 3.0 的专用提示词（Seedance 2.5 仍由 compile_video.build_prompt 写）。

每家按官方格式写（来源与核实日期见 docs/13）：
  海螺 H3   官方 skill h3-prompt-writing 的 Ref2VA 六段式英文：subject_definitions / summary / retention_analysis /
            detailed_description / overall_soundscape / non_diegetic_music；标签 <Subject N> <Video N>；
            镜头 [Shot 1] …、[Shot N] At MM:SS.mmm, …；说话人 (S1)，台词 <d>[Chinese] …</d>
  万相 3.0  官方公式：总体描述 / 参考素材（图N 视频N）/ 分镜N（00:00-00:04）+ 硬切转场 / 台词（角色说道："…"，口型同步）/
            音效与 BGM / 风格 / 负向清单
规格单里有结构化镜头（"struct"，设定.json + 分镜.json 生成）时，海螺版完全由结构化字段拼成英文；没有时退回中文正文并在检查里提示。
"""
import os
import re

ADJ_EN = {"大特写": "extreme close", "特写": "close", "近景": "medium close", "中景": "medium", "全景": "wide", "大全景": "extremely wide"}
COLOR_EN = {"黄": "yellow", "蓝": "blue", "红": "red", "粉": "pink", "绿": "green", "紫": "purple", "灰": "grey"}
SIZE_EN = {"大特写": "extreme close-up", "特写": "close-up", "近景": "medium close-up", "中景": "medium shot", "全景": "full shot", "大全景": "extreme wide shot"}
TYPE_EN = {"wide": "wide shot", "single": "single shot", "ots": "over-the-shoulder shot", "insert": "top-down insert close-up",
           "track": "tracking shot", "pov": "point-of-view shot"}
MOVE_EN = {"wide": "static camera", "single": "static camera", "ots": "static camera", "insert": "static camera",
           "track": "the camera tracks alongside the subject", "pov": "handheld point-of-view camera"}
NEG_WAN = "字幕、水印、画面里的文字和标志、多余的人物、白模里的纯色小人和灰色方块、卡通或动画质感、肢体变形、多余的手指、人物换脸"
NEG_WAN_TEXT = "字幕、水印、画面里的文字和标志、多余的人物、卡通或动画质感、肢体变形、多余的手指、多出来的胳膊、人物换脸、发型和服装中途改变"  # 不喂白模时不提白模
SHAPE_ZH = {"rig": "带关节的纯色小人", "cyl": "纯色圆柱小人", "giant": "树干形巨物代理"}
SHAPE_EN = {"rig": "solid-colored jointed mannequin", "cyl": "solid-colored cylinder figure", "giant": "trunk-shaped giant proxy"}


def previs_map(spec, ref_of, lang="zh", full=False):
    """白模的样子 + 每个代理换成谁（4.0 alpha.10，三家共用）。ref_of(tag) 返回这家对这张图的叫法，没有图返回 None。
    返回 (白模长什么样, [代理 → 谁], [没有参考图的代理])。没有 proxies 字段的旧规格单退回 previs_colors。"""
    look = spec.get("previs_look") or {}
    prox = spec.get("proxies")
    zh = lang == "zh"
    if prox is None:
        return ("灰色白模：识别色圆柱是人物，灰白方块是场景和道具" if zh else "grey whitebox previs: colored proxies are the characters, grey boxes are the set"), \
            [spec.get("previs_colors", "")], []
    shapes = sorted({p["shape"] for p in prox if p["shape"] != "giant"}) or ["rig"]
    if zh:
        desc = "白模是简化的三维预演：人物是" + "、".join(SHAPE_ZH[x] for x in shapes) + "，颜色只是识别色；墙、地面、家具是灰白方块"
        desc += "；白模里有方向光和影子，只取光从哪边来，光的颜色、强弱和画面质感按下面的风格" if look.get("lit") else "；白模是平光，不取它的光"
    else:
        desc = ("The previs is a simplified 3D blockout: people are " + " and ".join(SHAPE_EN[x] + "s" for x in shapes)
                + " whose colors are only ID colors; walls, floor and furniture are grey-white boxes")
        desc += ("; it has directional light and shadows, of which only the light direction is kept, while light color, strength and texture follow the style below"
                 if look.get("lit") else "; it is flat-lit and its light is not used")
    rows, missing = [], []
    for p in prox:
        ref = ref_of(p["tag"])
        if zh:
            who = (f"{ref}「{p['name']}」" + (f"（长相、发型、服装、体型全按{ref}）" if full else "")) if ref else f"「{p['name']}」"
            rows.append(f"{p['color']}色{SHAPE_ZH[p['shape']]} → {who}")
        else:
            who = f"{ref} ({p['name']})" if ref else p["name"]
            rows.append(f"the {COLOR_EN.get(p['color'], p['color'])} {SHAPE_EN[p['shape']]} → {who}")
        if not ref:
            missing.append(p["name"])
    crowd = ref_of("@crowd")
    if look.get("extras"):
        rows.append(("浅米色小人 → " + (f"{crowd}群像里按编号的群演" if crowd else "背景里的路人")) if zh
                    else ("the beige figures → " + (f"the numbered extras from {crowd}" if crowd else "background passers-by")))
    scene = ref_of("@loc")
    rows.append(("灰白方块 → " + (f"{scene}里的墙、地面和家具" if scene else "场景里的墙、地面和家具")) if zh
                else ("grey-white boxes → " + (f"the walls, floor and furniture of {scene}" if scene else "the walls, floor and furniture of the set")))
    if look.get("props"):
        rows.append("有尺寸的灰白小块 → 道具（只取位置和动作）" if zh else "small sized grey blocks → the props (position and motion only)")
    if look.get("hands"):
        rows.append("肉色小方块 → 特写里的手" if zh else "skin-colored small blocks → the hands in close-ups")
    if look.get("effects"):
        rows.append("跳动的小石块、飘落的小方片、张开的深色缝、伸长的长条 → 特效（只取位置、方向和时机）" if zh
                    else "bouncing chips, falling flakes, opening dark cracks and extending bars → the effects (position, direction and timing only)")
    return desc, rows, missing


def ref_lookup(assets, label_of):
    """按资产标签找这家的叫法：@人名、@crowd（群像）、@loc（第一张场景图）。"""
    def f(tag):
        for i, a in enumerate(assets, 1):
            if tag == a.get("tag") or (tag == "@crowd" and a["kind"] == "crowd") or (tag == "@loc" and str(a.get("tag", "")).startswith("@loc_")):
                return label_of(i, a)
        return None
    return f


def fmt(x):
    return f"{x:g}"


def mmss(t):
    m = int(t // 60)
    return f"{m:02d}:{t - m * 60:06.3f}"


def has_struct(shots):
    return all("struct" in s for s in shots)


def quotes(text):
    """规格单正文 → 万相：台词 {…} 改成引号；<音效> 是 Seedance 写法，改成"（音效：…）"。"""
    text = re.sub(r"\{([^}]*)\}", r"“\1”", text)
    text = re.sub(r"(?:<[^<>]+>\s*)+", lambda m: "（音效：" + "、".join(re.findall(r"<([^<>]+)>", m.group(0))) + "）", text)
    return text.replace("；（音效", "（音效").replace("）。", "）。")


# ---------------- 海螺 H3（官方 Ref2VA 六段式） ----------------

def hailuo(spec, part, short_duty_fn):
    t0, t1, shots = part
    dur = t1 - t0
    assets = spec["assets"]
    label, defs, keep = {}, [], []
    for i, a in enumerate(assets, 1):
        lab = f"<Subject {i}>"
        label[a["name"]] = lab
        en = a.get("brief_en", "")
        if a["kind"] == "character":
            defs.append(f"{lab}: {a['name']} — {en} Appearance from uploaded image {i} (face only from the large portrait on the right; "
                        "the blurred oval on the full-body view is not a face; backdrop and sheet layout not carried over).")
            keep.append(f"{lab}: fully_preserved — face, hair, costume and body shape")
        elif a["kind"] == "giant":
            defs.append(f"{lab}: {a['name']} — {en} Shape, materials, colors and scale from uploaded design sheet {i} "
                        "(backdrop and sheet layout not carried over).")
            keep.append(f"{lab}: fully_preserved — silhouette, materials, colors and scale")
        elif a["kind"] == "crowd":
            defs.append(f"{lab}: crowd extras — {en}. They come from uploaded reference image {i}, a lineup sheet of 14 numbered people "
                        "(row 1 left to right = 1–7, row 2 = 8–14); only the numbered people named below appear, each at most once, "
                        "and none of them is a main character. The white backdrop and the lineup layout are not carried over.")
            keep.append(f"{lab}: attribute_transfer — faces, costumes and props of the numbered extras")
        elif a["kind"] == "first":
            defs.append(f"{lab}: the approved first frame of this clip, uploaded image {i}. The opening composition, the people's look, "
                        "the set and the light start exactly as in this image, then move as the timeline describes.")
            keep.append(f"{lab}: fully_preserved — opening composition, look and light")
        elif a["kind"] == "tail":
            defs.append(f"{lab}: the last frame of the previous clip, uploaded image {i}. It carries over only where each person stands, "
                        "which way they face, their pose, the state of their clothes, and the light, so this clip continues from it; "
                        "it is not this clip's first frame and its image quality is not copied.")
            keep.append(f"{lab}: partially_preserved — positions, facing, pose, costume state and light")
        elif a["kind"] == "prop":
            defs.append(f"{lab}: {a['name']} — {en} Shape and material come from uploaded reference image {i}; the grey backdrop is not carried over.")
            keep.append(f"{lab}: fully_preserved — shape, material and state")
        else:
            defs.append(f"{lab}: {a['name']} — {en} Spatial layout, materials and light come from uploaded reference image {i}; "
                        "it is not a first frame and its composition is not copied.")
            keep.append(f"{lab}: partially_preserved — layout, materials and lighting")
    desc, rows, _ = previs_map(spec, ref_lookup(assets, lambda i, a: label[a["name"]]), "en")
    defs.append("<Video 1>: the previs of this clip. It defines only the camera path, cut points, framing, blocking and the order of events; the proxies' poses only say who does what and when — generate natural human motion, never copy their stiff poses, sliding or mechanical arm swings. "
                f"{desc}. Each proxy is replaced by real content: " + "; ".join(rows) + ". Only real people, sets and props appear in the target video.")
    for a in spec.get("_audio", []):  # 音色参考（sound 阶段生成）
        who = label.get(a["name"], a["name"])
        defs.append(f"{a['ref']}: voice of {who} (timbre and accent only, not its words).")
        keep.append(f"{a['ref']}: attribute_transfer — voice of {who}")
    keep.append("<Video 1>: weak_reference — camera path, cut timing, framing and blocking only")

    desc = [f"Live-action cinematic footage. {spec.get('style_en', '')}".strip(),
            "Every shot change is a hard cut with no fades or dissolves; each shot keeps one camera position and one main camera move. "
            "There are no subtitles and no on-screen text."]
    sids, sounds, warn = {}, [], []
    for k, sh in enumerate(shots, 1):
        st = sh.get("struct")
        a, b = sh["t0"] - t0, sh["t1"] - t0
        head = f"[Shot {k}]" if k == 1 else f"[Shot {k}] At {mmss(a)}, hard cut to a"
        if not st:  # 退回中文正文
            warn.append(sh["id"])
            desc.append(f"{head} shot matching {fmt(a)}–{fmt(b)} s of <Video 1>: {quotes(sh['body'])}")
            continue
        typ = st["type"]
        sz = st.get("size")
        if typ == "single":
            frame = f"{SIZE_EN[sz]} single shot" if sz else "single shot"
        elif typ == "wide":
            frame = SIZE_EN[sz] if sz and sz in ("全景", "大全景") else "wide shot"
        else:
            frame = f"{ADJ_EN[sz]} {TYPE_EN[typ]}" if sz else TYPE_EN[typ]
        lens = f"{st['lens']} mm lens"
        if k == 1:
            frame = frame[0].upper() + frame[1:]
        seen = []
        if typ == "ots":
            seen.append(f"{label.get(st['fg'], st['fg'])}'s blurred shoulder in the foreground")
        if typ == "insert":
            seen.append(f"{st.get('hand_en') + ' and ' if st.get('hand_en') else ''}{st.get('object_en') or st.get('object')}")
        for n in ([st.get("subject")] if st.get("subject") and typ != "insert" else []) + [w for w in (st.get("who") or []) if w not in (st.get("subject"), st.get("fg"))]:
            seen.append(label.get(n, n))
        if st.get("crowd"):
            cl = next((label[a2["name"]] for a2 in assets if a2["kind"] == "crowd"), "the crowd lineup")
            ids = st["crowd"]["ids"]
            seen.append(f"crowd extras number {', '.join(map(str, ids[:-1])) + ' and ' + str(ids[-1]) if len(ids) > 1 else ids[0]} from {cl}, "
                        f"{st['crowd'].get('doing_en', '')}".rstrip(", "))
        if typ == "pov":
            seen.insert(0, f"what {label.get(st['subject'], st['subject'])} sees")
        mv_en = st["move"]["en"] if st.get("move") else MOVE_EN[typ]
        text = f"{head} {frame} on a {lens}, {mv_en}, matching {fmt(a)}–{fmt(b)} s of <Video 1>. In frame: {'; '.join(seen)}."
        for act in st.get("action", []):
            en = act.get("en") or act.get("zh")
            text += f" At {mmss(a + act['at'])}, {en}." if act.get("at") is not None else f" {en[0].upper() + en[1:]}."
        for key in ("hit_en", "vfx_en", "acting_en"):  # 武戏受力因果、特效七要素、表演泄漏（crew/08 三轴）
            if st.get(key):
                text += f" {st[key][0].upper() + st[key][1:].rstrip('.')}."
        ln = st.get("line")
        if ln:
            who = label.get(ln["who"], ln["who"])
            first = ln["who"] not in sids
            sids.setdefault(ln["who"], f"S{len(sids) + 1}")
            voice = next((a2.get("voice_en") for a2 in assets if a2["name"] == ln["who"]), "") or spec.get("voice_en", {}).get(ln["who"], "")
            ident = f"{who} ({sids[ln['who']]})" + (f", {voice}," if first and voice else "")
            mode = ln.get("mode", "on")
            verb = {"on": "says", "back": "seen from behind in the foreground, says",
                    "off": "says in an off-screen voiceover while every on-screen speaker's lips remain closed"}[mode]
            dl = f", {ln['delivery_en']}" if ln.get("delivery_en") else ""
            text += f" {ident} {verb}{dl}: <d>[Chinese] {ln['text']}</d> Only the speaker's lips move."
        for sd in st.get("sound", []):
            if sd.get("en") and sd["en"] not in sounds:
                sounds.append(sd["en"])
        desc.append(text)
    snd = ("Throughout the clip: " + ", ".join(sounds) + ".") if sounds else "Quiet room tone with the natural sounds of the actions on screen."
    music = spec.get("music_en") or "N/A"
    task = "[reference generation]"
    summary = f"{task} A {fmt(dur)}-second live-action clip. {spec.get('summary_en') or spec['summary']}"
    if dur != spec["duration"]:
        summary += f" This is seconds {fmt(t0)}–{fmt(t1)} of clip {spec['clip']}, generated on its own and joined in the edit."
    out = ["subject_definitions:", *defs, "", "summary:", summary, "", "retention_analysis:", *keep, "",
           "detailed_description:", "\n".join(desc), "", "overall_soundscape:", snd, "", "non_diegetic_music:", music]
    return "\n".join(out), warn


def english_words(text):
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))


# ---------------- 万相 3.0（官方公式） ----------------

WAN_LENS = {18: "18mm 超广角，深焦", 24: "24mm 广角，深焦", 28: "28mm 广角", 35: "35mm 镜头", 50: "50mm 标准镜头",
            85: "85mm 长焦，浅景深", 135: "135mm 长焦，浅景深"}  # 官方写法是焦段 + 景深；不写视场角度数（"84°"会被当成机位角度）
WAN_MOVE = {"wide": "固定镜头，摄影机静止", "single": "固定镜头，摄影机静止", "ots": "固定镜头，摄影机静止", "insert": "固定镜头，俯拍特写",
            "track": "跟拍，镜头跟着人物平移", "pov": "手持主观视角"}
WAN_PITCH = {"高机位俯拍": "高机位往下俯拍（约 45° 俯角，看得到人群的头顶和后背）", "低机位仰拍": "低机位往上仰拍", "平视": "平视，机位和人眼一样高"}
QUIET = r"耳语|气声|小声|悄|压得?很低|压低|很轻|轻声"
LOUD = r"喊|吼|大声|尖叫"


def wan_volume(delivery):
    """括号里的语气模型常常不听，音量单独写成一句看得见的身体（docs/14 4.3 第 8 条）。"""
    if re.search(QUIET, delivery or ""):
        return "音量很小，几乎是气声耳语，明显比这一段里别人说话轻；低着头，嘴唇只微微动。"
    if re.search(LOUD, delivery or ""):
        return "音量很大，喊出来，比这一段里别人说话响得多。"
    return ""


def wan_shot(sh, a, k, who_ref):
    """不喂白模时的一个分镜：时间段 + 景别 / 焦段 / 俯仰 / 一个运镜 + 画面里谁在哪 + 动作（整段的绝对秒数）+ 表演 + 台词 + 音效。"""
    st = sh.get("struct")
    b = a + (sh["t1"] - sh["t0"])
    if not st:  # 项目不是 设定.json + 分镜.json 写的：退回中文正文
        return f"分镜{k}（{fmt(a)}-{fmt(b)}秒）：{quotes(sh['body'])}"
    lay = st.get("layout") or {}
    typ = st["type"]
    size = st.get("size") or {"wide": "全景", "insert": "特写"}.get(typ, "中景")
    if typ == "ots":
        size = f"{size}过肩镜头（越过{st.get('fg')}的肩膀拍{st.get('subject')}）"
    lens = WAN_LENS.get(st.get("lens"), f"{st.get('lens')}mm 镜头")
    cam = (st.get("move") or {}).get("zh") or WAN_MOVE.get(typ, "固定镜头，摄影机静止")
    pitch = "" if typ == "insert" else WAN_PITCH.get(lay.get("pitch", "平视"), lay.get("pitch", "平视")) + "，"
    parts = [f"{size}，{lens}，{pitch}{cam}"]
    seen = []
    if typ == "insert":
        if st.get("hand"):
            seen.append(f"只拍{st['hand']}的特写，画框里看不到人脸；这只手出画再伸回来也还是这只手，袖口和手腕上的东西不变")
        else:
            seen.append(f"只拍{st.get('object') or '物件'}的特写")
    fg = st.get("fg") if typ == "ots" else None
    poses = st.get("poses") or {}
    if fg:
        fr = next((r for r in lay.get("people", []) if r["who"] == fg), None)
        seen.append(f"{fg}背对镜头，肩膀和后脑在{fr['side'] if fr else '画面一侧'}前景，虚化")
    ppl = sorted([r for r in lay.get("people", []) if r["who"] != fg], key=lambda r: r["x"])
    if len(ppl) >= 2:
        seen.append("从画面左到右：" + "、".join(r["who"] for r in ppl))
    for r in ppl:
        seen.append(f"{r['who']}在{r['side']}{r['near']}" + (f"，{poses[r['who']]}" if poses.get(r["who"]) else "") + (f"，{r['move']}" if r["move"] else ""))
    if not ppl and typ != "insert":
        names = [n for n in ([st.get("subject")] + list(st.get("who") or [])) if n and n != fg]
        if names:
            seen.append("画面里：" + "、".join(dict.fromkeys(names)))
    if st.get("crowd"):
        cr = lay.get("crowd") or {}
        seen.append(f"群演一共 {len(st['crowd'].get('ids', []))} 个人（不多不少）：{st['crowd'].get('doing', '')}" + (f"，整体{cr['move']}" if cr.get("move") else ""))
    if seen:
        parts.append("；".join(seen))
    acts = []
    for x in st.get("action", []):
        if x.get("zh"):
            acts.append(f"第 {fmt(round(a + x['at'], 2))} 秒，{x['zh']}" if x.get("at") is not None else x["zh"])
    said = "".join(acts)
    acts += [f"第 {fmt(round(a + bt['t'], 2))} 秒，{bt['text']}" for bt in st.get("beats") or []
             if bt.get("text") and bt["text"][-6:] not in said]  # 节拍和动作说的是同一件事就不重复（重复会被演两遍）
    if acts:
        parts.append("；".join(acts))
    if st.get("acting"):
        parts.append(f"表演：{st['acting']}")
    ln = st.get("line")
    if ln:
        ref = who_ref(ln["who"])
        name = f"{ref}（{ln['who']}）" if ref else ln["who"]
        how = f"（{ln['delivery']}）" if ln.get("delivery") else ""
        say = {"on": f"{name}说{how}", "back": f"背对镜头的{name}说{how}", "off": f"画外音，{name}说{how}"}[ln.get("mode", "on")]
        parts.append(f"{say}：“{ln['text']}”")  # 官方写法：xx说：“内容”
        vol = wan_volume(ln.get("delivery"))
        if vol:
            parts.append(vol)
    else:
        parts.append("这一镜无台词，画面里的人都不说话")
    snd = "、".join(x["zh"] for x in st.get("sound", []) if x.get("zh"))
    if snd:
        parts.append(f"（音效：{snd}）")
    text = "".join(p_ if p_.endswith(("。", "”", "）")) else p_ + "。" for p_ in parts)
    return f"分镜{k}（{fmt(a)}-{fmt(b)}秒）：{text}"


def wan(spec, part, short_duty_fn):
    """万相 3.0 官方公式：总体描述 / 参考素材 / 分镜 / 台词 / 音效与BGM / 风格 / 负向清单。
    默认不喂白模（2026-11-09 用户决定，先只靠提示词）：镜头、站位、姿势全写进分镜；带白模的写法保留，WAN_PREVIS=1 时用。"""
    if os.environ.get("WAN_PREVIS") == "1":
        return wan_previs(spec, part, short_duty_fn)
    t0, t1, shots = part
    assets = spec["assets"]
    refs = {a["name"]: f"图{i}" for i, a in enumerate(assets, 1) if a["kind"] in ("character", "giant")}
    who_ref = refs.get
    out = []
    if len(shots) == 1:
        out.append("生成单镜头，一镜到底，中途不切。")
    out += ["总体描述：" + spec["context"] + spec["count"].replace('每一镜画面里有谁，以时间轴里"画面里"那一句为准。', "每一镜画面里有谁，以分镜里写的为准。")]
    if spec.get("blocking"):
        out.append(f"空间布局（整段不变）：{spec['blocking']}")
    if spec.get("poses"):
        out.append("人物状态（每一镜都保持，不换姿势、不换发型）：" + "；".join(f"{n}{v}" for n, v in spec["poses"].items()) + "。")
    out.append("动作质感：脚步踩实地面，步幅和速度像真人；动作克制自然，不夸张；每个分镜写的动作做完就停住，保持那个姿势直到切镜，不自己加动作、不加手势。")
    out += ["", "参考素材："]
    for i, a in enumerate(assets, 1):
        out.append(short_duty_fn(a, f"图{i}"))
    out += [f"{a['ref']}是「{a['name']}」的音色参考：只取音色、音高、语调和口音，不取内容和背景声。" for a in spec.get("_audio", [])]
    out += ["", "分镜（每个分镜一个机位、一个主运镜，按时间硬切；景别、焦段和运镜按每个分镜写的来，不自行增加镜头）：" if len(shots) > 1
            else "镜头（一个机位、一个主运镜，景别、焦段和运镜按下面写的来）："]
    for k, sh in enumerate(shots, 1):
        line = wan_shot(sh, sh["t0"] - t0, k, who_ref)
        out.append(line + ("硬切转场。" if k < len(shots) else ""))
    spoken = [(sh["struct"]["line"]) for sh in shots if (sh.get("struct") or {}).get("line")]
    if spoken:
        rows = [f"{(who_ref(l['who']) + '（' + l['who'] + '）') if who_ref(l['who']) else l['who']}：“{l['text']}”" for l in spoken]
        voices = "；".join(v.rstrip("。") for v in spec["voices"])
        tl = "台词（一字不改，按顺序）：" + "；".join(rows) + "　一次只有一个人说话，只有说话的人动嘴，其余人闭嘴。口型同步。" + (f"音色：{voices}。" if voices else "")
    else:
        tl = "台词：无台词。"
    sfx = "、".join(re.findall(r"<([^<>]+)>", spec["sfx"])) or "只有画面里动作的自然声"
    no_bgm = re.search(r"没有配乐|无配乐|无 ?bgm", spec["music"])
    bodies = " ".join(sh["body"] for sh in shots if (sh.get("struct") or {}).get("type") == "insert")
    cons = re.sub(r"道具操作（([^）]+)）：[^道]*?(?=道具操作（|$)", lambda m: m.group(0) if m.group(1) in bodies else "", spec["constraint"])  # 只留特写里真用到的道具操作，其余会被演出来（docs/14 4.3 第 14 条）
    out += ["", tl,
            "", "音效与BGM：" + sfx + "。" + ("无 bgm，只生成环境音和动作音。" if no_bgm else spec["music"]),
            "", "风格：" + spec["style"] + spec["two_worlds"],
            "", "约束：" + cons,
            "", "负向清单：" + NEG_WAN_TEXT]
    if t1 - t0 != spec["duration"]:
        out.insert(0, f"（本段是第 {spec['clip']} 条的第 {fmt(t0)}–{fmt(t1)} 秒，单独生成，剪辑时按顺序接回去。）")
    return "\n".join(out), [sh["id"] for sh in shots if not sh.get("struct")]


def wan_previs(spec, part, short_duty_fn):
    """带白模的万相写法（官方"白模参考 / 渲染"）。默认不用；WAN_PREVIS=1 时用。"""
    t0, t1, shots = part
    assets = spec["assets"]
    lines = [quotes(x) for sh in shots for x in re.findall(r"\{[^}]*\}", sh["body"])]
    out = ["总体描述：" + spec["context"] + spec["count"], "", "参考素材："]
    for i, a in enumerate(assets, 1):
        out.append(short_duty_fn(a, f"图{i}"))
    desc, rows, _ = previs_map(spec, ref_lookup(assets, lambda i, a: f"图{i}"), full=True)
    out += ["", "白模参考（视频1）：",
            "【保持不变】以视频1为唯一蓝本：镜头路线、剪辑点、构图、时长、人物的位置、走位、动作先后和主光方向，逐个分镜对齐，一帧不增删、一秒不错位。",
            f"【白模写实化】{desc}。逐个替换：" + "；".join(rows) + "。",
            "【成片】只出现真实的人、场景和道具；人物的长相、发型、体型和服装按对应的图，全程保持一致；画面里不残留任何白模区域、纯色小人或灰色方块。"]
    out += [f"{a['ref']}是「{a['name']}」的音色参考：只取音色、音高、语调和口音，不取内容和背景声。" for a in spec.get("_audio", [])]
    out += ["", "分镜（每个分镜一个机位、一个主运镜，按时间硬切）："]
    for k, sh in enumerate(shots, 1):
        a, b = sh["t0"] - t0, sh["t1"] - t0
        tail = "硬切转场。" if k < len(shots) else ""
        body = re.sub(r"^(画面里：[^。]*。)\d+°", lambda m: m.group(1) + WAN_LENS.get((sh.get("struct") or {}).get("lens") or sh.get("lens"), f"{(sh.get('struct') or {}).get('lens') or sh.get('lens')}mm 镜头"), sh["body"])  # 视场角度数换成焦段写法
        out.append(f"分镜{k}（{fmt(a)}-{fmt(b)}秒）：{quotes(body)}{tail}")  # 官方写法"（起-止）"；不截成整秒
    sfx = "、".join(re.findall(r"<([^<>]+)>", spec["sfx"])) or "只有画面里动作的自然声"
    out += ["", "台词：" + ("台词一字不改；一次只有一个人说话，只有说话的人动嘴。口型同步。" + "；".join(v.rstrip("。") for v in spec["voices"]) + "。" if lines else "无台词。"),
            "", "音效与BGM：" + sfx + "。" + ("无 bgm。" if re.search(r"没有配乐|无配乐|无 ?bgm", spec["music"]) else spec["music"]),
            "", "风格：" + spec["style"] + spec["two_worlds"],
            "", "约束：" + spec["constraint"],
            "", "负向清单：" + NEG_WAN]
    if t1 - t0 != spec["duration"]:
        out.insert(0, f"（本段是第 {spec['clip']} 条的第 {fmt(t0)}–{fmt(t1)} 秒，单独生成，剪辑时按顺序接回去。）")
    return "\n".join(out), []


BUILD = {"hailuo": hailuo, "wan": wan}
