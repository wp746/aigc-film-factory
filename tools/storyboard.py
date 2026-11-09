#!/usr/bin/env python3
"""分镜数据 → 资产文档 + 白模规格 + 镜头规格单（docs/19）。

智能体只填两份 JSON：
  <项目>/设定.json   片名、世界与风格配方、人物、道具、场景（含白模站位）、群像
  <项目>/分镜.json   每条：地点、谁在哪、每镜的类型 / 景别 / 主体 / 台词 / 动作 / 声音
机位坐标、"画面里"的写法、说话人写法、群演点名、过肩轴线、素材清单都由本工具按固定规则算出来，
所以同一份数据交给任何智能体，产物都一样。

  python3 tools/storyboard.py check <项目>     只校验，列出错误和改法（不写文件）
  python3 tools/storyboard.py assets <项目>    校验 + 写 交付/01-资产提示词.md
  python3 tools/storyboard.py specs <项目>     校验 + 写 预演规格/第N条.json 和 交付/规格单/第N条.json
"""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import projectkit as K  # noqa: E402

REC = "Image 2.5（APIMart 2K）"
SIZES = {"大特写": (135, 0.6, 0.93), "特写": (85, 1.3, 0.92), "近景": (50, 1.4, 0.88), "中景": (50, 2.4, 0.8),
         "全景": (35, 5.0, 0.6), "大全景": (24, 11.0, 0.55)}  # 景别 → (焦距, 机位距离 米, 机位高度占身高比例)
TYPES = ("wide", "single", "ots", "insert", "track", "pov")
MOVE = {"wide": "固定", "single": "固定", "ots": "过肩", "insert": "俯拍特写", "track": "跟拍", "pov": "主观视角"}
# 有动机的运镜（docs/20 第四节"张力"）：kind → (中文, 英文)
MOVES = {"push": ("推近", "the camera pushes in"), "pull": ("拉远", "the camera pulls back"),
         "truck_left": ("向左横移", "the camera trucks left"), "truck_right": ("向右横移", "the camera trucks right"),
         "rise": ("升起", "the camera cranes up"), "drop": ("降下", "the camera cranes down"),
         "orbit_left": ("向左环绕", "the camera arcs left around the subject"), "orbit_right": ("向右环绕", "the camera arcs right around the subject"),
         "handheld": ("手持，轻微晃动", "handheld, with a slight unsteady sway")}
SPEEDS = {"slow": ("缓慢", "slowly"), "steady": ("匀速", "steadily"), "sudden": ("突然", "suddenly")}
SIZE_RANK = {"大特写": 0, "特写": 1, "近景": 2, "中景": 3, "全景": 4, "大全景": 5}


def move_text(mv, lang="zh"):
    kind, sp = MOVES[mv["kind"]], SPEEDS.get(mv.get("speed", "slow"), SPEEDS["slow"])
    if mv["kind"] == "handheld":
        return kind[0] if lang == "zh" else kind[1]
    if lang == "zh":
        return f"镜头{sp[0]}{kind[0]}" + (f"到{mv['to']}" if mv.get("to") else "") + (f"，跟随{mv['follow']}" if mv.get("follow") else "")
    return f"{kind[1]} {sp[1]}" + (f", following {mv['follow_en']}" if mv.get("follow_en") else "")


def shot_rank(s):
    if s.get("size") in SIZE_RANK:
        return SIZE_RANK[s["size"]]
    return {"wide": 4, "insert": 1}.get(s.get("type"), 2 if s.get("type") == "single" else 3)
PALETTE = [[0.95, 0.8, 0.15], [0.2, 0.35, 0.85], [0.85, 0.2, 0.15], [0.95, 0.6, 0.75], [0.3, 0.75, 0.4], [0.6, 0.35, 0.8]]
PALETTE_ZH = ["黄", "蓝", "红", "粉", "绿", "紫"]
# 精准白模（4.0 alpha.8）：节拍里能用的动作，和 tools/previs/common.py 的 POSES 一致
POSE_NAMES = {"stand", "walk", "run", "sit", "sit_ground", "kneel", "kowtow", "bend", "crouch", "pick", "raise_arms", "carry", "hands_front",
              "lean_back", "dragged", "fall", "nod", "comb", "pull", "struggle", "laugh", "reach", "grab", "give", "point", "hold", "look",
              "punch", "jab", "kick", "block", "duck", "stagger", "lie", "slump"}  # 打戏与倒地（4.0 alpha.12）
PROP_DOS = {"take", "put", "move", "swing", "show", "hide"}
FORMATIONS = {"line", "rows", "column", "ring", "scatter"}
BOXCOLOR = {"wood": [0.35, 0.25, 0.18], "white": [0.92, 0.92, 0.9], "stone": [0.45, 0.47, 0.5], "floor": [0.5, 0.5, 0.52],
            "dark": [0.12, 0.12, 0.14], "red": [0.85, 0.12, 0.1], "gold": [1.0, 0.75, 0.2], "glass": [0.75, 0.8, 0.9],
            "skin": [0.86, 0.68, 0.56]}
LIP = ("对白：普通话，自然口语。所有角色只说花括号里的台词；每句台词只有说话的那一个角色动嘴，其余角色闭着嘴；画外音不对任何人的口型；"
       "人声干净贴近，有人说话时配乐和环境声自动压低。画面全程是纯影像，没有字幕。")
ANCHOR = "人物都是正常的人体结构：两只手各五根手指。"
SAVIOR = re.compile(r"金手指|系统(发|奖|签到)|签到|认亲|亲孙子|赘婿|战神|龙王|跪地求|求复合|一夜暴富|神豪")  # docs/20 第一节：等救主结构
SAVIOR_SOFT = re.compile(r"豪门|重生复仇|打脸")  # 4.0 alpha.9：主流母题本身不拦（docs/23 第一节、docs/11 货架 E），只提醒查一遍主角是不是自己选择、付了代价
TAGS = ("铺垫", "推进", "压", "放", "反转", "揭示", "钩", "余韵")  # 功能标签（docs/23 第五节第 6 条）
HOOK_TYPES = ("生死悬停", "身份揭晓", "半句话", "更大对手", "反常陈述", "宣战", "认知颠覆", "亲密打断", "喜剧定格", "物证特写", "时间戳循环", "规则宣告")  # docs/23 第六节
SPEED_TIERS = ("静", "慢", "常速", "快", "极速")  # 打戏速度档（crew/17 第 5 节）
FEED = re.compile(r"你忘了吗|你忘了.{0,6}吗|还记得.{0,10}吗|三年前|五年前|十年前|你明明知道|我是.{1,8}(集团|总裁|董事长|少爷|大小姐)")  # 灌输式台词（docs/23 第四节第 7 条）
CORE_KEYS = {"want": "主角想要什么", "need": "他真正需要什么", "choice": "关键时刻他自己做的选择", "cost": "这个选择让他失去什么",
             "image": "能说出主题的那个画面"}
ACTION_RE = re.compile(r"打斗|出拳|挥拳|一拳|踢向|踢中|飞踢|挥刀|砍向|刺向|格挡|摔倒|扑倒|撞(上|向|飞|倒)|躲闪|翻滚|扭打|掐住|推倒|追赶|追逐|搏斗|过肩摔|踩塌|砸向|砸碎|甩向|甩飞|一脚踹")  # 武戏：要写受力因果 hit
VFX_RE = re.compile(r"爆炸|火焰|魔法|法术|能量|光束|光柱|变身|闪电|结界|传送|粒子|发光|光环|符文|召唤|尘浪|水墙|巨浪|孢子|冲击波")  # 特效：要写七要素 vfx
HIT_KEYS = {"support": "支撑（脚踩在哪、重心在哪）", "force": "发力（从哪发力、朝哪）", "contact": "接触（打在哪、碰到什么）",
            "reaction": "受力（被打的一方怎么动）", "result": "结果（停在什么状态）"}
VFX_KEYS = {"source": "源点", "direction": "方向", "scale": "尺度（和人比多大）", "speed": "速度", "medium": "介质反馈（空气、水、尘土怎么动）",
            "light": "环境照明（照亮了哪里、什么颜色）", "decay": "衰减与残留"}


def end_mark(cv):
    return cv["path"][-1]["at"] if "path" in cv else cv.get("at")


def start_mark(cv):
    return cv["path"][0]["at"] if "path" in cv else cv.get("at")


CROWD_WORDS = re.compile(r"路人|人群|群众|顾客们|村民|行人|正式工们|士兵们|客人们|观众")
HAND_WORDS = re.compile(r"手|拇指|食指|指尖|掌心")
SEP = re.compile(r"[，。！？、…\s\.,!?—~～「」“”\"'（）()]")


class Bad(Exception):
    pass


def _rd(path):
    if not os.path.exists(path):
        raise Bad(f"缺少 {os.path.basename(path)}（格式见 docs/19、docs/22，样例 examples/样例-便利店/）")
    try:
        return json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Bad(f"{os.path.basename(path)} 不是合法 JSON：{e}")


def series_kind(proj):
    """剧集模式（docs/22）："season" = 剧目录（有 季.json），"episode" = 集目录（父目录有 季.json），None = 单片。"""
    if os.path.exists(os.path.join(proj, "季.json")):
        return "season"
    if os.path.exists(os.path.join(os.path.dirname(proj), "季.json")) and not os.path.exists(os.path.join(proj, "设定.json")):
        return "episode"
    return None


def episode_dirs(season_root):
    season = _rd(os.path.join(season_root, "季.json"))
    return [(e["ep"], os.path.join(season_root, f"ep{e['ep']}")) for e in season.get("episodes", [])]


_SHOT_T = re.compile(r"^镜(\d+)(?:([+-])(\d+(?:\.\d+)?)|(末))?$")
_T_KEYS = ("t", "t0", "t1", "open_at", "tear_from", "ahead_until", "face_until")


def _shot_times(board):
    """走位、群演、道具、特效的时间可以写成"镜3+1.2"（第 3 镜开始后 1.2 秒）、"镜3-0.5"（第 3 镜结束前 0.5 秒）、"镜3末"。
    改镜头时长时走位跟着镜头走，不会因为时间写死而把人挤成快进（《好日子》教训）。这里换算成整条的秒数。"""
    for c in board.get("clips", []):
        starts, t = [], 0.0
        for sh in c.get("shots", []):
            starts.append((t, t + sh.get("dur", 0)))
            t += sh.get("dur", 0)

        def conv(v, c=c, starts=starts):
            if not isinstance(v, str):
                return v
            m = _SHOT_T.match(v.strip())
            if not m:
                return v
            k = int(m.group(1))
            if not 1 <= k <= len(starts):
                raise Bad(f"第{c.get('n')}条 时间「{v}」：没有第 {k} 镜")
            a, b = starts[k - 1]
            if m.group(4):
                return round(b, 3)
            if not m.group(2):
                return round(a, 3)
            x = float(m.group(3))
            return round(a + x if m.group(2) == "+" else b - x, 3)

        def walk(o):
            if isinstance(o, dict):
                for k2, v in o.items():
                    if k2 in _T_KEYS:
                        o[k2] = conv(v)
                    elif k2 == "back" and isinstance(v, list):
                        o[k2] = [conv(x) for x in v]
                    elif k2 not in ("shots",):
                        walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        for key in ("cast", "extras", "prop_states", "effects"):
            if key in c:
                walk(c[key])
    return board


def load(proj):
    bible, board = _load_raw(proj)
    if board.get("_series", {}).get("mode") == "season":
        for _ep, b in board["_series"]["episodes"]:
            _shot_times(b)
    return bible, _shot_times(board)


def _load_raw(proj):
    kind = series_kind(proj)
    if kind == "episode":
        root = os.path.dirname(proj)
        bible, board = _rd(os.path.join(root, "设定.json")), _rd(os.path.join(proj, "分镜.json"))
        board["_series"] = {"mode": "episode", "season": _rd(os.path.join(root, "季.json")), "ep": os.path.basename(proj)[2:], "root": root}
        return bible, board
    if kind == "season":  # 全季合并：只用来出全季资产文档和全季检查
        bible, season = _rd(os.path.join(proj, "设定.json")), _rd(os.path.join(proj, "季.json"))
        clips, eps = [], []
        for ep, d in episode_dirs(proj):
            p = os.path.join(d, "分镜.json")
            if not os.path.exists(p):
                continue
            b = _rd(p)
            eps.append((ep, b))
            for i, c in enumerate(b.get("clips", [])):
                c = dict(c, n=f"E{ep}-{c['n']}")
                if i == 0:
                    c.setdefault("time_jump", "换集")
                clips.append(c)
        return bible, {"clips": clips, "_series": {"mode": "season", "season": season, "episodes": eps, "root": proj}}
    return _rd(os.path.join(proj, "设定.json")), _rd(os.path.join(proj, "分镜.json"))


def char_map(bible):
    """人物 + 状态变体（变体名也能进 cast，身高、音色、识别色随本人；docs/22 状态卡）。"""
    m = {c["name"]: c for c in bible.get("characters", [])}
    for c in bible.get("characters", []):
        for k, v in enumerate(c.get("variants", []), 1):
            m[v["name"]] = dict(c, name=v["name"], lock=v.get("lock", c.get("lock", "")), _base=c["name"], _stage=k,
                                back_view=v.get("back_view", False))
    return m


def base_name(bible, name):
    return char_map(bible).get(name, {}).get("_base", name)


# ---------------- 校验 ----------------

def models_of(proj):
    p = os.path.join(proj, "production.json")
    return json.load(open(p, encoding="utf-8")).get("video_models", ["seedance"]) if os.path.exists(p) else ["seedance"]


def validate(bible, board, models=("seedance",)):
    """返回 (errors, warnings)，每条都带"怎么改"。models 里有 hailuo 时，额外要求英文字段（H3 官方提示词是英文分段格式）。"""
    E, W = [], []
    need_en = "hailuo" in models
    EN = "（要出海螺 H3：官方提示词是英文分段格式，英文由你在数据里写一次，编译器不临场翻译）"
    recipes = {r["id"] for r in json.load(open(os.path.join(ROOT, "config", "style_recipes.json"), encoding="utf-8"))["配方"]}
    for k in ("title", "short", "film", "worlds", "people_recipe", "three", "music", "characters", "locations"):
        if k not in bible:
            E.append(f"设定.json 缺字段 {k}")
    if E:
        return E, W
    core = bible.get("core") or {}
    for k, zh in CORE_KEYS.items():  # docs/20 第三节内核闸门 A–E
        if len(str(core.get(k, "")).strip()) < 4:
            E.append(f"设定.json 的 core.{k} 没写或太短：{zh}（docs/20 第三节；写不出来说明故事内核还没立住，回到故事阶段）")
    if core and core.get("choice") and re.search(r"被|等|得到|获得|认出|揭晓", str(core.get("choice"))[:6]):
        W.append("core.choice 读起来像是主角在等或被动得到；关键选择要是他主动做的（docs/20 第三节 A）")
    hit = SAVIOR.search(" ".join(str(x) for x in [core, bible.get("three", ""), bible.get("title", "")]))
    if hit:
        E.append(f"故事内核里出现「{hit.group(0)}」：这是等救主结构（docs/20 第一节、第三节 F），本工厂不做；回到故事阶段换内核")
    soft = SAVIOR_SOFT.search(" ".join(str(x) for x in [core, bible.get("three", ""), bible.get("title", "")]))
    if soft:
        W.append(f"故事内核里有「{soft.group(0)}」：主流母题可以用（docs/11 货架 E），但查一遍——赢是主角自己选择、付了代价换来的，不是身份揭晓或别人出手（docs/20 第三节 F）")
    for k, zh in (("promise", "情绪承诺：观众看的是什么感"), ("dilemma", "两难：做 A 失 X，不做失 Y")):
        if core and len(str(core.get(k, "")).strip()) < 4:
            W.append(f"core.{k} 没写：{zh}（docs/23 第一节，4.0 alpha.9 起建议写）")
    for w, v in bible["worlds"].items():
        if v.get("recipe") not in recipes:
            E.append(f"世界「{w}」的 recipe {v.get('recipe')} 不在 config/style_recipes.json（用 tools/mj_style.py --recipes 看）")
    if bible["people_recipe"] not in recipes:
        E.append(f"people_recipe {bible['people_recipe']} 不存在")
    for w, v in bible["worlds"].items():  # 4.0 alpha.8：每个世界可以有自己的人物配方（白昼和黄昏的人不该写同一种光）
        if v.get("people") and v["people"] not in recipes:
            E.append(f"世界「{w}」的 people {v['people']} 不在 config/style_recipes.json")
    if need_en and not bible.get("music_en"):
        E.append("设定.json 缺 music_en：配乐的英文一两句（没有配乐写 \"N/A\"）" + EN)
    chars = char_map(bible)
    props = {p["name"]: p for p in bible.get("props", [])}
    locs = {l["name"]: l for l in bible["locations"]}
    crowds = {c["world"]: c for c in bible.get("crowds", [])}
    for c in bible["characters"]:
        if c.get("kind") not in (None, "giant", "voice"):
            E.append(f"人物「{c.get('name')}」的 kind 只能是 giant（巨物：怪兽、机甲、巨人，出巨物设定图）或 voice（只有声音：电话、广播、对讲机，不出图、不入画）")
        if c.get("kind") == "voice":
            for k in ("voice",) + (("voice_en",) if need_en else ()):
                if k not in c:
                    E.append(f"人物「{c.get('name')}」缺 {k}" + (EN if k == "voice_en" else ""))
            continue
        if c.get("kind") == "giant" and c.get("recipe", bible.get("giant_recipe", "R15")) not in recipes:
            E.append(f"巨物「{c.get('name')}」的 recipe {c.get('recipe', bible.get('giant_recipe', 'R15'))} 不存在")
        for k in ("lock", "brief_en", "must_en", "height", "voice") + (("voice_en",) if need_en else ()):
            if k not in c:
                E.append(f"人物「{c.get('name')}」缺 {k}" + (EN if k == "voice_en" else ""))
        if len(str(c.get("silhouette", ""))) < 4:
            E.append(f"人物「{c.get('name')}」缺 silhouette：剪影识别点，只看轮廓就认得出是谁（发型、体型、标志物件）（crew/07）")
        if re.search(r"毛孔|瑕疵|颗粒|pores?|blemish|grain", c.get("lock", "") + c.get("must_en", ""), re.I):
            E.append(f"人物「{c.get('name')}」描述里写了毛孔 / 瑕疵 / 颗粒：出图模型会照字面画成砂纸皮肤和噪点（docs/16 第二节），改写成具体的雀斑、痣、细纹和光泽")
        if re.search(r"\d+\s*岁|岁的|小孩|孩子|少年|少女", c.get("lock", "")):
            E.append(f"人物「{c['name']}」描述锁写了年龄词，改成'二十出头的成年人'这类写法（不写具体岁数、不写未成年）")
    for l in bible["locations"]:
        for k in ("world", "lock", "en", "must_en", "stage"):
            if k not in l:
                E.append(f"场景「{l.get('name')}」缺 {k}")
        if l.get("world") not in bible["worlds"]:
            E.append(f"场景「{l.get('name')}」的 world 不在 worlds 里")
        for k, zh in (("light", "光逻辑：主光是什么、在哪、什么色温，辅光从哪来"), ("era", "时代锁：可以出现 / 不能出现的物件"),
                      ("wear", "使用痕迹：被谁用过、怎么磨损（磨亮的扶手、墙角水渍……）")):
            if len(str(l.get(k, ""))) < 6:
                E.append(f"场景「{l.get('name')}」缺美术字段 {k}：{zh}（crew/06，docs/20 美术指导）")
        if len(str(l.get("contrast", ""))) < 6:
            E.append(f"场景「{l.get('name')}」缺 contrast：这个场景的对冲，明暗 / 冷暖 / 大小 / 新旧 / 静动至少一组（docs/20 第四节\"张力\"）")
        if not l.get("anchor"):
            E.append(f"场景「{l.get('name')}」缺 anchor：锚点物列表，观众记得住、模型钉得住的 1–3 件东西（crew/06）")
        st = l.get("stage", {})
        if "establish" not in st or "marks" not in st:
            E.append(f"场景「{l.get('name')}」的 stage 要有 marks（站位点）和 establish（全景机位）")
    for c in bible.get("crowds", []):
        if len(c.get("roster", [])) != 14:
            E.append(f"群像「{c.get('world')}」roster 要正好 14 人（现在 {len(c.get('roster', []))}）")
        for i, r in enumerate(c.get("roster", []), 1):
            if len(r) != 2 or "cm tall" not in r[1]:
                E.append(f"群像「{c.get('world')}」第 {i} 人要写成 [中文一句, 英文一句含 'about NNN cm tall']（docs/17）")
        if c.get("world") not in bible["worlds"]:
            E.append(f"群像的 world「{c.get('world')}」要是 worlds 里的一个世界")
        for n in c.get("refs", []):
            if n not in chars:
                E.append(f"群像「{c.get('world')}」refs 里的 {n} 不是人物")
    if E:
        return E, W
    for p in bible.get("props", []):
        used = [s for c in board.get("clips", []) if p["name"] in c.get("props", []) for s in c.get("shots", [])
                if s.get("type") == "insert" or p["name"] in json.dumps(s.get("action", ""), ensure_ascii=False)]
        if used and len(str(p.get("mechanism", ""))) < 6:
            E.append(f"道具「{p['name']}」会被手操作，缺 mechanism：手怎么动 → 道具怎么响应 → 结果状态（crew/06 道具操作机制；不写会出现手在动、东西不动）")
    for clip in board.get("clips", []):
        tag = f"第{clip.get('n')}条"
        loc = clip.get("location")
        if loc not in locs:
            E.append(f"{tag} location「{loc}」不在设定的 locations 里")
            continue
        marks = locs[loc]["stage"]["marks"]
        cast = clip.get("cast", {})
        for name, cv in cast.items():
            if name not in chars:
                E.append(f"{tag} cast 里的「{name}」不是设定里的人物")
            elif chars[name].get("kind") == "voice":
                E.append(f"{tag} 「{name}」是只有声音的角色（kind: voice），不能进 cast；他的台词写 mode: off")
            if cv.get("ride"):  # 坐在车里（4.0 alpha.12）
                st_ = clip.get("prop_states", {}).get(cv["ride"], {})
                if cv["ride"] not in clip.get("props", []) or not (st_.get("path") or st_.get("at")):
                    E.append(f"{tag} 「{name}」ride 的「{cv['ride']}」要在这一条的 props 里，并在 prop_states 写 at 或 path")
                if "path" in cv or "at" in cv:
                    E.append(f"{tag} 「{name}」坐在车里（ride），不要再写 at / path：位置跟着车走，座位偏移写 seat")
            for p in ([cv] if "at" in cv else cv.get("path", [])):
                if isinstance(p.get("at"), str) and p["at"] not in marks:
                    E.append(f"{tag} 「{name}」的站位「{p['at']}」不在场景「{loc}」的 marks 里")
        for p in clip.get("props", []):
            if p not in props:
                E.append(f"{tag} props 里的「{p}」不在设定的 props 里")
            elif props[p].get("vehicle"):  # 载具的识别色撞了路面就看不见（高难镜头测试：黑车压在黑路上）
                ground = {bx.get("color", "white") for bx in locs[loc]["stage"].get("boxes", []) if bx["max"][2] <= 0.1
                          and min(bx["max"][0] - bx["min"][0], bx["max"][1] - bx["min"][1]) > 2}  # 大面积的路面、地面（不算路中的白线）
                if props[p].get("proxy_color", "white") in ground:
                    W.append(f"{tag} 载具「{p}」的 proxy_color「{props[p].get('proxy_color', 'white')}」和路面同色，白模里看不见：换一种颜色（只影响白模）")
        shots = clip.get("shots", [])
        turn, sig = clip.get("turn") or {}, clip.get("signature") or {}
        if not (turn.get("from") and turn.get("to")) or turn.get("from") == turn.get("to"):
            E.append(f"{tag} 缺价值翻转 turn: {{\"from\": 开头的处境, \"to\": 结尾的处境}}，两头要相反（docs/20 第四节）")
        if not isinstance(sig.get("shot"), int) or not 1 <= sig["shot"] <= len(shots) or len(str(sig.get("why", ""))) < 6:
            E.append(f"{tag} 缺视听高光 signature: {{\"shot\": 第几镜, \"why\": 构图、光、运动、声音怎么同时发力}}（docs/20 第四节）")
        if len(str(clip.get("tension", ""))) < 8:
            E.append(f"{tag} 缺压力源 tension：谁想要什么、什么挡着、观众比角色多知道还是少知道什么（docs/20 第四节\"张力\"）")
        en_ = clip.get("energy") or {}
        if not all(isinstance(en_.get(k), int) and 1 <= en_[k] <= 5 for k in ("in", "out")):
            E.append(f"{tag} 缺能量 energy: {{\"in\": 1–5, \"out\": 1–5}}：这一条开头和结尾的情绪强度，用来排全片曲线（docs/20 第四节\"张力\"）")
        speakers = []
        for s in shots:
            w = (s.get("line") or {}).get("who")
            if w and w not in speakers:
                speakers.append(w)
        if len(speakers) >= 2:
            dr = clip.get("drama") or {}
            k = dr.get("shift")
            if len(str(dr.get("a", ""))) < 4 or len(str(dr.get("b", ""))) < 4 or not isinstance(k, int):
                E.append(f"{tag} 有 {len(speakers)} 个人说话，是对手戏，要写 drama: {{\"a\": 一方想要什么, \"b\": 另一方想要什么, \"shift\": 优势易手那句台词在第几镜}}（crew/22 文戏导演）")
            elif not 1 <= k <= len(shots) or not shots[k - 1].get("line"):
                E.append(f"{tag} drama.shift={k} 要指向一个有台词的镜头")
            elif shots[k - 1].get("type") == "wide":
                E.append(f"{tag} drama.shift 那一镜是全景：优势易手的那句话要给最好的镜头（近景、过肩或推近），不给全景（crew/22）")
        durs = [s.get("dur", 0) for s in shots if s.get("dur")]
        if len(durs) >= 3 and max(durs) < 1.5 * min(durs):
            E.append(f"{tag} 镜头时长 {durs} 太平均，节奏四平八稳：高光镜头给足时长，之前用短镜头加速，最长至少是最短的 1.5 倍（docs/20 第四节）")
        moving = [s for s in shots if s.get("move") or s.get("type") in ("track", "pov") or (s.get("cam") or {}).get("end")]
        if shots and not moving and len(str(clip.get("stillness", ""))) < 6:
            E.append(f"{tag} 整条都是固定机位：给转折那一镜一个有动机的 move（推近、横移揭示、手持），或写 stillness 说明为什么要静、静之后哪里爆发（docs/20 第四节）")
        ranks = {shot_rank(s) for s in shots}
        if len(shots) >= 3 and len(ranks) < 3:
            W.append(f"{tag} 只用了 {len(ranks)} 种景别，每条至少三种（docs/20 第五节分镜师）")
        for k in range(1, len(shots)):
            a, b = shots[k - 1], shots[k]
            if a.get("type") == b.get("type") and shot_rank(a) == shot_rank(b) and not b.get("move"):
                E.append(f"{tag} 镜{k} 和镜{k + 1} 类型和景别都一样，像跳切：换景别或角度（docs/14 第三节）")
        if need_en and not clip.get("summary_en"):
            E.append(f"{tag} 缺 summary_en：这一条的英文一句话" + EN)
        total = sum(s.get("dur", 0) for s in shots)
        if not 4 <= total <= 30:
            E.append(f"{tag} 总时长 {total} 秒，要在 4–30 秒（超过就拆条）")
        pairs = set()
        for k, s in enumerate(shots, 1):
            st = f"{tag} 镜{k}"
            t = s.get("type")
            if t not in TYPES:
                E.append(f"{st} type 要是 {TYPES} 之一")
                continue
            mv = s.get("move")
            if mv:
                if mv.get("kind") not in MOVES:
                    E.append(f"{st} move.kind 要是 {list(MOVES)} 之一")
                elif mv.get("speed", "slow") not in SPEEDS:
                    E.append(f"{st} move.speed 要是 {list(SPEEDS)} 之一")
                if len(str(mv.get("why", ""))) < 4:
                    E.append(f"{st} 运镜要写动机 move.why：跟视线、跟情绪，还是为了揭示什么；说不出动机就用固定机位（docs/20 第四节）")
                if t in ("track", "pov") or s.get("cam"):
                    E.append(f"{st} {t if not s.get('cam') else 'cam'} 镜头自带机位运动，不能再加 move")
                if mv.get("speed") == "sudden" and s.get("dur", 0) < 2:
                    E.append(f"{st} 突然的运镜单镜不短于 2 秒，模型才做得出来")
            if s.get("dur", 0) < 1.5:
                E.append(f"{st} 时长 {s.get('dur')} 秒，短于 1.5 秒模型做不出来，并到相邻镜头")
            if s.get("size") and s["size"] not in SIZES:
                E.append(f"{st} size 要是 {list(SIZES)} 之一")
            vis = set(s.get("who", []))
            for key in ("subject", "fg"):
                if s.get(key):
                    vis.add(s[key])
            for n in vis:
                if n not in cast:
                    E.append(f"{st} 「{n}」不在本条 cast 里（先在 cast 里给他站位）")
            if t in ("single", "ots", "track", "pov") and not s.get("subject"):
                E.append(f"{st} {t} 镜头要写 subject（拍谁）")
            if t == "ots":
                if not s.get("fg"):
                    E.append(f"{st} 过肩要写 fg（越过谁的肩膀）")
                else:
                    pairs.add(frozenset((s["fg"], s.get("subject"))))
                    a_, b_ = cast.get(s["fg"]), cast.get(s.get("subject"))
                    if a_ and b_ and "path" not in a_ and "path" not in b_ and a_.get("at") == b_.get("at"):
                        E.append(f"{st} 过肩的「{s['fg']}」和「{s.get('subject')}」站在同一个站位「{a_.get('at')}」，算不出机位：分开站")
            if t == "wide" and s.get("view") and s["view"] not in locs[loc]["stage"].get("views", {}):
                E.append(f"{st} view「{s['view']}」不在场景 stage.views 里")
            if t == "pov" and not s.get("look") and not s.get("looks"):
                E.append(f"{st} 主观镜头要写 look（看向哪个站位或人物）")
            if s.get("angle") and s["angle"] not in ("ground", "low", "high", "overhead"):
                E.append(f"{st} angle 要是 ground（贴地）/ low（低机位仰拍）/ high（高机位俯拍）/ overhead（顶拍）之一")
            cprops = set(clip.get("props", []))
            for n in s.get("see", []):
                if n not in cprops:
                    E.append(f"{st} see 里的「{n}」不在这一条的 props 里（see 只写画面里要拍到的车和道具）")
            cmo = (s.get("cam") or {}).get("mount")
            if cmo:
                if cmo.startswith("prop_") and cmo[5:] not in cprops:
                    E.append(f"{st} cam.mount「{cmo}」不在这一条的 props 里")
                elif not cmo.startswith("prop_") and cmo not in cast:
                    E.append(f"{st} cam.mount「{cmo}」既不是本条的人，也不是 prop_道具")
                if not s["cam"].get("look") and not s["cam"].get("looks"):
                    E.append(f"{st} 挂载机位要写 look 或 looks（看哪里）")
            for lk_ in s.get("looks", []) + (s.get("cam") or {}).get("looks", []):
                if "t" not in lk_ or "at" not in lk_ or not 0 <= lk_["t"] <= s.get("dur", 0):
                    E.append(f"{st} looks 每项写 {{\"t\": 镜内秒, \"at\": 看哪里}}，t 在 0 到这一镜时长之间")
            if t == "insert":
                if s.get("object") not in marks:
                    E.append(f"{st} 插入镜头的 object 要是场景 marks 里的点（拍哪个物件）")
                if s.get("action") and not s.get("hand") and not s.get("no_hand"):
                    E.append(f"{st} 插入镜头有动作，要写 hand：谁的手、什么样的手、袖口什么样（第 1 条实拍教训）；画面里确实没有手就写 no_hand: true")
            acts = s.get("action", [])
            acts = acts if isinstance(acts, list) else [acts]
            if need_en:
                for a in acts:
                    if not (isinstance(a, dict) and a.get("en")):
                        E.append(f"{st} 动作要写成 {{\"text\": 中文, \"en\": 英文}}（可加 \"at\": 秒）" + EN)
                for snd in s.get("sound", []):
                    if isinstance(snd, dict) and not snd.get("en"):
                        E.append(f"{st} 声音「{snd.get('text')}」缺 en" + EN)
                if s.get("crowd") and not s["crowd"].get("doing_en"):
                    E.append(f"{st} crowd 缺 doing_en" + EN)
                if t == "insert" and not s.get("object_en"):
                    E.append(f"{st} 插入镜头缺 object_en（拍的物件，英文）" + EN)
                if t == "insert" and s.get("hand") and not s.get("hand_en"):
                    E.append(f"{st} 插入镜头缺 hand_en（谁的手，英文）" + EN)
            line = s.get("line")
            if line:
                txt = line.get("text", "")
                n = len(SEP.sub("", txt))
                if n / s["dur"] > 4.5:
                    E.append(f"{st} 台词 {n} 字 / {s['dur']} 秒 = {n / s['dur']:.1f} 字/秒，超过 4.5，删字或加长镜头")
                mode = line.get("mode", "on")
                who = line.get("who")
                if who not in chars:
                    E.append(f"{st} 台词的 who「{who}」不是人物")
                elif chars[who].get("kind") == "voice" and mode != "off":
                    E.append(f"{st} 「{who}」只有声音（kind: voice），台词 mode 要写 off")
                elif mode == "on" and who not in vis:
                    E.append(f"{st} 说话人「{who}」不在画里：让他入画，或 mode 写 back（背对镜头 / 过肩前景）或 off（画外）")
                elif mode == "back" and who != s.get("fg"):
                    E.append(f"{st} mode=back 只用于过肩镜头的前景人物说话")
                if len(acts) > 1:
                    E.append(f"{st} 对白镜头最多一个动作（现在 {len(acts)} 个），其余挪到别的镜头（第 1 条实拍教训）")
                if not acts:
                    E.append(f"{st} 对白镜头要先写一个手上的动作（剥橘子、拉袖子、摘耳机……），模型会把动作演成说话的自然停顿；"
                             "没有动作的台词容易念得像背书（docs/20 台词对白、crew/23）")
                if len(SEP.sub("", txt)) > 12 and not re.search(r"[，、…—？！?!,]", txt.rstrip("。.？！?!")):
                    W.append(f"{st} 台词「{txt}」超过 12 个字中间没有停顿：用逗号、省略号、问号写出呼吸，或拆成半句 + 回问")
                if need_en and line.get("delivery") and not line.get("delivery_en"):
                    E.append(f"{st} 台词写了 delivery，缺 delivery_en" + EN)
            a_all = " ".join(a.get("text", "") if isinstance(a, dict) else a for a in acts)
            for key, rx, keys, who in (("hit", ACTION_RE, HIT_KEYS, "武戏导演 crew/17：逐击五步因果"), ("vfx", VFX_RE, VFX_KEYS, "VFX 导演 crew/18：特效七要素")):
                m = rx.search(a_all)
                if m or s.get(key):
                    obj = s.get(key) or {}
                    miss = [zh for k2, zh in keys.items() if len(str(obj.get(k2, ""))) < 2]
                    if miss:
                        E.append(f"{st} 动作里有「{m.group(0) if m else key}」，要写 {key}：{'、'.join(miss)}（{who}；不写模型会做成没有因果的乱动）")
                    elif need_en and not obj.get("en"):
                        E.append(f"{st} {key} 缺 en：一句英文" + EN)
            for a in acts:
                a_txt = a.get("text", "") if isinstance(a, dict) else a
                if CROWD_WORDS.search(a_txt) and not s.get("crowd"):
                    E.append(f"{st} 动作里写到了路人，要用 crowd 点群演编号（docs/17），并在设定里有该世界的群像")
            if s.get("crowd"):
                world = locs[loc]["world"]
                if world not in crowds:
                    E.append(f"{st} 用了群演，但设定里没有世界「{world}」的群像")
                elif any(not 1 <= i <= 14 for i in s["crowd"].get("ids", [])):
                    E.append(f"{st} 群演编号要在 1–14")
            for snd in s.get("sound", []):
                if not isinstance(snd, dict) or not snd.get("source"):
                    E.append(f"{st} 声音要写成 {{\"text\": 声音, \"source\": 画里的发声者 或 \"画外\"}}，模型只给看得见的声源配音（第 1 条实拍教训）")
                elif snd["source"] != "画外" and snd["source"] not in vis and snd["source"] not in marks and not s.get("crowd"):
                    W.append(f"{st} 声音「{snd['text']}」的声源「{snd['source']}」不在画里，模型可能不出声")
        if len(pairs) > 1 and not clip.get("allow_axis_change"):
            E.append(f"{tag} 过肩用了 {len(pairs)} 组人物，轴线会乱；一条里只拍一组对话，或写 allow_axis_change: true 并在镜头里交代换轴")
    clips_ = board.get("clips", [])
    ens = [c.get("energy") or {} for c in clips_]
    vals = [e[k] for e in ens for k in ("in", "out") if isinstance(e.get(k), int)]
    if len(clips_) >= 2 and vals:
        if max(vals) < 4:
            E.append(f"全片能量最高只有 {max(vals)}：至少有一条到 4 以上，全片要有一个高点（docs/20 第四节\"张力\"）")
        if len(set(vals)) < 3:
            E.append(f"全片能量只有 {sorted(set(vals))} 这几档：曲线太平，要有起伏（压 → 升 → 爆 → 落）")
        outs = [e.get("out") for e in ens if isinstance(e.get("out"), int)]
        if len(outs) >= 3 and max(outs) <= 2:
            W.append("每一条都低能量收尾：观众没有理由看下一条，至少一条结尾停在高处或悬念上")
    if len(clips_) >= 3:
        avg = lambda c: sum(s.get("dur", 0) for s in c.get("shots", [])) / max(1, len(c.get("shots", [])))  # noqa: E731
        film = sum(avg(c) for c in clips_) / len(clips_)
        if not any(avg(c) <= 0.75 * film for c in clips_):
            W.append(f"全片没有加速段：每条平均镜长都接近 {film:.1f} 秒，高潮前至少有一条用更短的镜头（docs/20 第四节）")
    for a, b in zip(clips_, clips_[1:]):
        if a.get("location") != b.get("location") or b.get("time_jump"):
            continue
        for name in set(a.get("cast", {})) & set(b.get("cast", {})):
            e, st_ = end_mark(a["cast"][name]), start_mark(b["cast"][name])
            if e != st_:
                E.append(f"第{b.get('n')}条 「{name}」开头在「{st_}」，可是第{a.get('n')}条结尾在「{e}」：同一场连续的戏站位要接上；"
                         "中间确实跳过了时间就写 time_jump: \"跳过了什么\"（场记 crew/12，docs/07）")
    all_shots = [s for c in board.get("clips", []) for s in c.get("shots", [])]
    if all_shots and all(s.get("line") for s in all_shots):
        W.append("全片每一镜都有台词：至少留一段不靠台词、只用画面和声音讲的戏（docs/20 第二节第 5 条）")
    img_cap = {"seedance": 30, "hailuo": 9, "wan": 10}  # 各家参考图上限（docs/13）
    for c in board.get("clips", []):  # 白模里的每个人都要有参考图，否则模型会把代理原样画出来（4.0 alpha.10，万相实拍教训）
        people_n = sum(1 for n in c.get("cast", {}) if chars.get(n, {}).get("kind") != "voice")
        need = people_n + 1 + (1 if any(s.get("crowd") for s in c.get("shots", [])) else 0)
        for m in models:
            if m in img_cap and need > img_cap[m]:
                W.append(f"第{c.get('n')}条 有 {people_n} 个人加场景图，{m} 最多收 {img_cap[m]} 张参考图：多出来的人在白模里有代理却没有图，会被画成纯色小人；拆条或减人（docs/13 八·六）")
    for c in board.get("clips", []):  # 4.0 alpha.12，学自 scenario-labs：连着两个物件特写会泄掉张力；喂白模的条超过 15 秒布局容易漂
        sh_ = c.get("shots", [])
        for k in range(1, len(sh_)):
            if sh_[k].get("type") == "insert" and sh_[k - 1].get("type") == "insert" and not sh_[k].get("insert_pair"):
                W.append(f"第{c.get('n')}条 镜{k}、镜{k + 1} 连着两个特写：观众一直看不到人，张力会掉下来；中间插一个人的镜头，或合成一个特写（确实要连着拍，写 insert_pair: true）")
        total = sum(s.get("dur", 0) for s in sh_)
        if "seedance" in models and total > 15.01:
            W.append(f"第{c.get('n')}条 {total:g} 秒喂白模出：实测 10–15 秒的片段机位和布局都保得住，20–30 秒会漂（丢人、自己加特写）；能拆就在硬切处拆成两条（docs/14 4.3 第 1 条）")
    if "wan" in models:  # 万相默认不喂白模，空间全靠文字：两人以上同场要写场面布局（docs/14 4.3 第 5 条）
        for c in board.get("clips", []):
            seen = [n for n, v in c.get("cast", {}).items() if not v.get("hidden") and chars.get(n, {}).get("kind") != "voice"]
            if len(seen) >= 2 and not c.get("blocking"):
                W.append(f"第{c.get('n')}条 有 {len(seen)} 个人同场，万相不喂白模：写 blocking（谁坐谁站、面朝哪、离门窗家具多远、谁在门里谁在门外），给每人写 cast.pose（姿势、发型、手里的东西）")
    for c in board.get("clips", []):  # hidden：在场但看不见的人（坐在轿里、躲在柜子里）
        for n, v in c.get("cast", {}).items():
            if "hidden" not in v:
                continue
            if not isinstance(v["hidden"], str) or len(v["hidden"]) < 3:
                E.append(f"第{c.get('n')}条 「{n}」的 hidden 要写一句她在哪、为什么看不见，如 \"坐在花轿里，轿帘放下\"（会写进提示词）")
            for k, sh in enumerate(c.get("shots", []), 1):
                ln = sh.get("line") or {}
                if n in (sh.get("who") or []) or n in (sh.get("subject"), sh.get("fg")) or (ln.get("who") == n and ln.get("mode", "on") != "off"):
                    E.append(f"第{c.get('n')}条 镜{k}「{n}」写了 hidden（看不见），却出现在画面里；说话只能 mode: off")
    script_check(bible, board, E, W)
    action_check(board, W)
    look_check(bible, board, E, W)
    ai_weak_check(board, W)
    leak_check(bible, board, E)
    emotion_check(board, E)
    for c in board.get("clips", []):
        for k, sh in enumerate(c.get("shots", []), 1):
            for key in ("allow_occlude", "allow_small", "allow_exit"):
                if key in sh and not isinstance(sh[key], list):
                    E.append(f"第{c.get('n')}条 镜{k} {key} 要写成名字列表，如 [\"程姐\"]（写 true 会让白模检查崩溃）")
    precise = any(sh.get("beats") for c in board.get("clips", []) for sh in c.get("shots", []))
    bprops = {p["name"]: p for p in bible.get("props", [])}
    crowd_worlds = {c["world"] for c in bible.get("crowds", [])}
    for c in board.get("clips", []):  # 精准白模（4.0 alpha.8）：节拍、群演、道具状态
        loc = next((l for l in bible.get("locations", []) if l["name"] == c.get("location")), {})
        marks = loc.get("stage", {}).get("marks", {})
        ex_ids = {i for g in c.get("extras", []) for i in g.get("ids", [])}
        who_ok = set(c.get("cast", {})) | {f"群演{i}" for i in ex_ids}
        for g in c.get("extras", []):
            if g.get("formation", "line") not in FORMATIONS:
                E.append(f"第{c.get('n')}条 extras 的 formation 只能是 {sorted(FORMATIONS)}")
            if any(not 1 <= i <= 14 for i in g.get("ids", [])):
                E.append(f"第{c.get('n')}条 extras 的群演编号要在 1–14（对应群像图）")
            for q in [g.get("at")] + [x.get("at") for x in g.get("path", [])]:
                if q and isinstance(q, str) and q not in marks:
                    E.append(f"第{c.get('n')}条 extras 用了不存在的站位点「{q}」")
            if g.get("world", loc.get("world")) not in crowd_worlds:  # 远景里的同一批人可以借别的世界的群像（g.world），不必为几个小点再出一张图
                E.append(f"第{c.get('n')}条 有 extras，但世界「{g.get('world', loc.get('world'))}」没有群像（docs/17）；远景小点可写 world 借用别的世界的群像")
            for q in g.get("poses", []):
                if q.get("pose") not in POSE_NAMES:
                    E.append(f"第{c.get('n')}条 extras 的姿态「{q.get('pose')}」不在动作表里：{sorted(POSE_NAMES)}")
        for nm, st in c.get("prop_states", {}).items():
            if nm not in bprops:
                E.append(f"第{c.get('n')}条 prop_states 里的道具「{nm}」不在设定的 props 里")
        for k, sh in enumerate(c.get("shots", []), 1):
            tag = f"第{c.get('n')}条 镜{k}"
            for bt in sh.get("beats", []):
                if not 0 <= bt.get("t", -1) <= sh.get("dur", 0):
                    E.append(f"{tag} 节拍时间 {bt.get('t')} 不在这一镜的 0–{sh.get('dur')} 秒里（节拍时间从这一镜开头算）")
                if bt.get("prop"):
                    if bt["prop"] not in bprops:
                        E.append(f"{tag} 节拍里的道具「{bt['prop']}」不在设定的 props 里")
                    elif not bprops[bt["prop"]].get("size"):
                        W.append(f"{tag} 道具「{bt['prop']}」没写 size: [宽, 深, 高]（米），白模按默认 0.3×0.2×0.1 做")
                    if bt.get("do") not in PROP_DOS:
                        E.append(f"{tag} 道具节拍的 do 只能是 {sorted(PROP_DOS)}")
                    if bt.get("do") in ("take", "put", "move") and not bt.get("to"):
                        E.append(f"{tag} 道具节拍 {bt.get('do')} 要写 to（take：人名.右手；put / move：站位点名）")
                elif bt.get("who"):
                    if bt["who"] not in who_ok:
                        E.append(f"{tag} 节拍里的「{bt['who']}」不在这一条的 cast 或 extras 里")
                    if bt.get("do") not in POSE_NAMES:
                        E.append(f"{tag} 节拍动作「{bt.get('do')}」不在动作表里：{sorted(POSE_NAMES)}")
                else:
                    E.append(f"{tag} 节拍要写 who + do（人做什么）或 prop + do（道具怎么动）")
            if precise and not sh.get("beats") and sh.get("type") != "wide":
                acts = sh.get("action", [])
                txt = " ".join(a.get("text", "") if isinstance(a, dict) else a for a in (acts if isinstance(acts, list) else [acts]))
                if re.search(r"攥|抓|递|塞|拿|捡|推|拉|拽|跪|坐|弯腰|举|扑|咬|梳|放进|掀|挑|抬", txt):
                    W.append(f"{tag} 动作里有手上或身体的动作，但没写 beats：白模不会演，模型会自由发挥")
    for c in board.get("clips", []):  # 后期字段（tools/post.py）
        tr = c.get("transition")
        if tr is not None and tr not in ("cut", "black", "dissolve"):
            E.append(f"第{c.get('n')}条 transition 只能是 cut / black / dissolve，现在是 {tr!r}")
        ts = c.get("transition_sec")
        if ts is not None and not (isinstance(ts, (int, float)) and 0.1 <= ts <= 2):
            E.append(f"第{c.get('n')}条 transition_sec 要写 0.1–2 之间的秒数")
        if c.get("grade") not in (None, "keep"):
            E.append(f"第{c.get('n')}条 grade 只能写 \"keep\"（这一条后期不调色）")
        if "light_change" in c and len(str(c["light_change"])) < 4:
            E.append(f"第{c.get('n')}条 light_change 写清剧情里光怎么变（如\"怪兽影子压过来，整条街暗一档\"），后期就不会把它当成模型变暗补回来")
    cl = board.get("clips", [])
    for a, b in zip(cl, cl[1:]):
        if a.get("location") == b.get("location") and not b.get("time_jump") and a.get("model") and b.get("model") and a["model"] != b["model"]:
            W.append(f"第{a.get('n')}条和第{b.get('n')}条是同一场连续的戏，模型却不同（{a['model']} / {b['model']}）：同一场戏不换模型，换模型只在硬切处（docs/13）")
    fl = bible.get("film_look")
    for cid in [(fl or {}).get("camera")] + [w.get("camera") for w in bible.get("worlds", {}).values()] + \
            [s.get("camera") for c in board.get("clips", []) for s in c.get("shots", [])]:
        if cid and cid not in CAMS:
            E.append(f"摄影机预设「{cid}」不在 config/camera_presets.json 里（可选：" + "、".join(f"{k} {v['name']}" for k, v in CAMS.items()) + "）")
    if (fl or {}).get("camera") and re.search(r"ARRI|Alexa|ALEXA|ARRICAM|RED |V-RAPTOR|Sony|VENICE|Panavision|阿莱|索尼", str(fl.get("zh", "")) + str(fl.get("en", ""))):
        W.append("film_look 选了摄影机预设，zh / en 里就不要再写机身：两套器材会互相打架；影调这一行只写调色、色板、雾气和光（docs/16 第六节）")
    if fl is not None and not (isinstance(fl, dict) and ((len(str(fl.get("zh", ""))) >= 6 and len(str(fl.get("en", ""))) >= 12) or fl.get("camera"))):
        E.append("film_look 要写 {\"zh\": 中文一行, \"en\": English one line, \"lens_zh\": \"变形宽银幕\"（可选）}：全片影调，逐字进场景图和视频提示词（docs/16 第六节）")
    if not fl and bible.get("look"):
        W.append("设定.json 没有 film_look（全片影调：摄影机、镜头组、调色、高光、雾气）：先用 tools/look_test.py 试拍 2–3 套再定（docs/16 第六节；《好日子》质感复盘）")
    if not bible.get("look") and not board.get("_series", {}).get("mode") == "episode":
        W.append("设定.json 没有 look：4.0 立项先挑类型卡和时长档位（docs/21），写成 \"look\": {\"genre\": \"G18\", \"tier\": \"1-3\"}")
    if board.get("_series"):
        series_check(bible, board, E, W)
    return E, W


def script_check(bible, board, E, W):
    """剧本层（4.0 alpha.9，docs/23）：伏笔回收、功能标签、台词节奏和灌输句式。只有伏笔顺序报错，其余提醒。"""
    clips = board.get("clips", [])
    order = [str(c.get("n")) for c in clips]
    for f in bible.get("foreshadow", []):
        p, q = str(f.get("plant", "")), str(f.get("payoff", ""))
        if board.get("_series"):
            continue  # 剧集的伏笔在 季.json 里按集查
        if p not in order or q not in order:
            E.append(f"伏笔「{f.get('what')}」的 plant / payoff（{p} → {q}）不是分镜里的条号")
        elif order.index(q) < order.index(p):
            E.append(f"伏笔「{f.get('what')}」回收（第{q}条）在埋设（第{p}条）之前")
        elif p == q:
            W.append(f"伏笔「{f.get('what')}」在同一条里埋下又回收：观众来不及记住，至少隔一条")
    tags = []
    for c in clips:
        t = c.get("tag")
        if t is not None and t not in TAGS:
            W.append(f"第{c.get('n')}条 tag「{t}」不在功能标签里：{'/'.join(TAGS)}")
        tags.append(t)
    for i in range(2, len(tags)):
        if tags[i] and tags[i] == tags[i - 1] == tags[i - 2]:
            W.append(f"第{clips[i - 2].get('n')}–{clips[i].get('n')}条 连续三条都是「{tags[i]}」：节奏单调，中间换一个放、反转或揭示（docs/23 第五节）")
            break
    for c in clips:
        lens_ = [len(SEP.sub("", t)) for t in _lines(c)]
        for i in range(2, len(lens_)):
            a = lens_[i - 2:i + 1]
            if min(a) >= 5 and max(a) - min(a) <= 1:
                W.append(f"第{c.get('n')}条 连续三句台词都是 {a[0]} 字左右：节奏死板，长短交错，或让一句变成回问一个词（docs/23 第四节）")
                break
        for t in _lines(c):
            m = FEED.search(t)
            if m:
                W.append(f"第{c.get('n')}条 台词「{t}」里有「{m.group(0)}」：像灌输（明知故说、自报家门），改成动作、道具或第三人说出来（docs/23 第四节第 7 条）")


def action_check(board, W):
    """打戏节奏（4.0 alpha.9，crew/17 第 5 节，来自《列阵》）：速度档、慢镜比例、最重一击的位置、重击后的反应镜、表演泄漏。只提醒。"""
    lv = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
    clips = board.get("clips", [])
    for c in clips:
        shots = c.get("shots", [])
        for k, s in enumerate(shots, 1):
            ac = s.get("acting")
            if ac:
                try:
                    pr, am, ct = (int(ac.get(x, 0)) for x in ("pressure", "amplitude", "control"))
                except (TypeError, ValueError):
                    W.append(f"第{c.get('n')}条 镜{k} acting 的 pressure / amplitude / control 要写 1–5 的整数")
                    continue
                if ct >= 4 and pr >= 4 and len(str(ac.get("leak", ""))) < 4:
                    W.append(f"第{c.get('n')}条 镜{k} 高压力（{pr}）+ 高控制（{ct}）：要写 acting.leak，情绪从哪里漏出来（下眼睑一跳、喉结滚一下、手停在半空），否则会演成面瘫（crew/08）")
                if am > pr:
                    W.append(f"第{c.get('n')}条 镜{k} 外在幅度（{am}）大于内在压力（{pr}）：演过了，会像表情包（crew/08 三轴）")
            sp = s.get("speed")
            if sp is not None and sp not in SPEED_TIERS:
                W.append(f"第{c.get('n')}条 镜{k} speed「{sp}」要是 {'/'.join(SPEED_TIERS)} 之一")
            w = lv.get((s.get("hit") or {}).get("weight"))
            if w and w >= 3 and k == len(shots) and not c.get("splice_reaction"):
                W.append(f"第{c.get('n')}条 镜{k} 是 {s['hit']['weight']} 重击，又是这一条最后一镜：每次重击要给两镜（接触点 + 受力方的反应），后面补一个反应镜（crew/17 镜头规则）")
        sps = [s.get("speed") for s in shots]
        for i in range(2, len(sps)):
            if sps[i] and sps[i] == sps[i - 1] == sps[i - 2]:
                W.append(f"第{c.get('n')}条 镜{i - 1}–{i + 1} 连续三镜都是「{sps[i]}」速度档：同一档最多连续两镜（crew/17 第 5 节）")
                break
    # 动作段：连着的、有 hit 的条（同一场）合起来算
    runs, cur = [], []
    for c in clips:
        has = any(s.get("hit") for s in c.get("shots", []))
        if has and cur and cur[-1].get("location") == c.get("location"):
            cur.append(c)
        else:
            if cur:
                runs.append(cur)
            cur = [c] if has else []
    if cur:
        runs.append(cur)
    for run in runs:
        seq = [s for c in run for s in c.get("shots", [])]
        total = sum(s.get("dur", 0) for s in seq)
        name = f"第{run[0].get('n')}" + (f"–{run[-1].get('n')}" if len(run) > 1 else "") + "条"
        slow = sum(s.get("dur", 0) for s in seq if s.get("speed") == "慢")
        if total and slow / total > 0.12:
            W.append(f"{name} 动作段里慢镜占 {slow / total:.0%}：慢镜不超过 12%，只给接触点、表情、道具这种信息帧（crew/17 第 5 节）")
        ws = [(lv.get((s.get("hit") or {}).get("weight"), 0), i) for i, s in enumerate(seq)]
        best = max(ws) if ws else (0, 0)
        if best[0] and total >= 8:
            t = sum(s.get("dur", 0) for s in seq[:best[1]]) + seq[best[1]].get("dur", 0) / 2
            if not 0.7 <= t / total <= 0.92:
                W.append(f"{name} 最重的一击落在动作段的 {t / total:.0%} 处：放到 75–90% 附近，前面留给试探和优势轮转，后面留给余波（crew/17 第 5 节）")


def _ref_names():
    names = set()
    for c in _cfg("genre_library.json")["类型"]:
        for r in c["参考"]:
            names |= {x.strip("《》 ") for x in re.findall(r"《[^》]+》", r)}
            names |= {x.strip() for x in re.split(r"[；;、/（）()《]", re.sub(r"《[^》]*》", "/", r)) if 2 <= len(x.strip()) <= 12 and "只学" not in x and "顶级" not in x and "改编" not in x}
    stop = {"爱死机", "docs", "11 #010", "工厂已有", "系列", "照明娱乐式 3D", "高畑勋的作品", "九十年代港片", "天劫外包",  # 说明文字、自家项目
            "回路", "燃烧", "潜伏", "放逐", "枪火", "异形", "招魂", "封神", "聊斋", "哪吒", "后窗", "侠女", "1917", "功夫", "密探"}  # 常用词、公版题材：不当片名拦
    return {n for n in names if len(n) >= 2 and n not in stop} | {"高畑勋"}


EMO_RX = (r"悲伤|痛苦|痛心|伤心|难过|绝望|崩溃|沮丧|失落|无奈|委屈|愧疚|内疚|羞愧|羞耻|嫉妒|思念|释然|欣喜|激动|感动|开心|高兴|快乐|兴奋|"
          r"愤怒|生气|恼火|紧张|害怕|恐惧|惊恐|恐慌|惶恐|焦虑|不安|尴尬|怀疑|心虚|得意|震惊|犹豫|迷茫|坚定|冷漠|温柔|慌|羞|恨|爱意")
CUE_RX = (r"眼|视线|眉|嘴|唇|下颌|下巴|喉|鼻|手|指|拳|头|肩|胸|背|腰|腿|脚|膝|呼吸|叹气|吸气|呼气|喘|眨|点头|摇头|歪|皱|抿|咬|靠|倾|摸|挠|抱|"
          r"停|僵|颤|抖|吞|笑|哭|泪|脸|攥|握|捏|抓|松开|退|走|跑|跪|坐|站|转身|回头|低头|抬头|声音|嗓|音量|语速|哑|哽|顿|沉默|轻声|大喊|喊|吼|小声|压低")


def emotion_check(board, E):
    """情绪只写成模型演得出来的动作（用户 2026-11-09：不写"他很悲伤""他很痛苦"）。
    动作、节拍文字、表演泄漏、台词语气会逐字进提示词：只写了情绪词、没写任何看得见或听得见的表现就报错。
    改法见 crew/08-acting-coach/真人对话数据.md 第二节、表演词表.md、SKILL.md C 节（情绪的起、到顶、收）。"""
    def bad(t):
        return bool(t) and re.search(EMO_RX, t) and not re.search(CUE_RX, t)
    for c in board.get("clips", []):
        for k, s in enumerate(c.get("shots", []), 1):
            tag = f"第{c.get('n')}条 镜{k}"
            acts = s.get("action", [])
            texts = [("动作", a.get("text") if isinstance(a, dict) else a) for a in (acts if isinstance(acts, list) else [acts])]
            texts += [("节拍", bt.get("text", "")) for bt in s.get("beats", []) or []]
            texts += [("表演泄漏", (s.get("acting") or {}).get("leak", "")), ("台词语气", (s.get("line") or {}).get("delivery", ""))]
            for kind, t in texts:
                if bad(t):
                    E.append(f"{tag} {kind}「{t}」只写了情绪：模型演不出情绪词，改成看得见的动作——哪里动、往哪动、多大、被什么触发"
                             f"（例：不写'他很悲伤'，写'听到那句话手上的动作停住，下巴往上一推，吞了一下'；crew/08 真人对话数据第二节、表演词表）")


def leak_check(bible, board, E):
    """类型卡里的导演、影片名只用于研究，不能进任何会编进提示词的字段（docs/21；版权与"不抄"红线）。"""
    names = _ref_names()
    data = json.dumps({k: v for k, v in bible.items() if k not in ("look", "core")}, ensure_ascii=False) + \
        json.dumps([{k: v for k, v in c.items() if k not in ("tension", "signature")} for c in board.get("clips", [])], ensure_ascii=False)
    hit = sorted(n for n in names if n in data)
    if hit:
        E.append(f"数据里出现了参考导演 / 影片名「{'、'.join(hit)}」：它们只能留在研究和故事稿里，提示词里改写成具体的光、色、构图、运动（docs/21）")


# ---------------- 剧集模式（docs/22） ----------------

def _lines(clip):
    return [s["line"]["text"] for s in clip.get("shots", []) if s.get("line")]


def series_check(bible, board, E, W):
    sr = board["_series"]
    season = sr["season"]
    cfg = season.get("series", {})
    eps = [e["ep"] for e in season.get("episodes", [])]
    chars = char_map(bible)
    if bible.get("ratio", "16:9") != cfg.get("ratio", bible.get("ratio", "16:9")):
        W.append(f"季.json 的画幅 {cfg.get('ratio')} 和 设定.json 的 ratio {bible.get('ratio', '16:9')} 不一致：以 设定.json 为准，改成一样")
    if sr["mode"] == "episode":
        ep = sr["ep"]
        if ep not in eps:
            E.append(f"ep{ep} 不在 季.json 的 episodes 里")
            return
        if board.get("ep") != ep:
            E.append(f"分镜.json 要写 \"ep\": \"{ep}\"（和目录名一致）")
        if len(str(board.get("hook", ""))) < 6:
            E.append("分镜.json 缺 hook：这一集结尾的钩子（一个画面或一句台词，停在观众最想看下一集的地方），要和 季.json 这一集的 hook 一致")
        if eps.index(ep) > 0 and len(str(board.get("opens_with", ""))) < 4:
            W.append("分镜.json 缺 opens_with：这一集开头怎么接住上一集的钩子（第 1 条前几秒，或冷开场之后）")
        clips = board.get("clips", [])
        total = sum(s.get("dur", 0) for c in clips for s in c.get("shots", []))
        lo, hi = cfg.get("ep_seconds", [0, 10 ** 6])
        if not lo <= total <= hi:
            W.append(f"这一集 {total:g} 秒，季设定每集 {lo}–{hi} 秒")
        words = sum(len(SEP.sub("", t)) for c in clips for t in _lines(c))
        dl, dh = cfg.get("dialogue_per_min", [0, 10 ** 6])
        if total and not dl <= words / (total / 60) <= dh:
            W.append(f"这一集台词 {words} 字，每分钟 {words / (total / 60):.0f} 字，季设定每分钟 {dl}–{dh} 字：太少撑不住一集，太多画面没有呼吸")
        byn = {c["n"]: c for c in clips}
        for c in clips:
            ref = c.get("cold_open_of")
            if ref:
                if ref not in byn:
                    E.append(f"第{c['n']}条 是冷开场，cold_open_of 指向的第{ref}条不存在")
                elif not set(_lines(c)) <= set(_lines(byn[ref])):
                    E.append(f"第{c['n']}条 是第{ref}条的冷开场（闪前），台词要一字不差地出现在第{ref}条里（后期用同一条录音）")
            for name in c.get("cast", {}):
                ch = chars.get(name, {})
                base = bible_char(bible, ch.get("_base", name))
                rv = base.get("reveal_ep") if base else None
                if rv and ep < rv and not ch.get("back_view"):
                    E.append(f"第{c['n']}条「{name}」：身份第 {rv} 集才揭开，之前只能用背影 / 遮脸的变体（variants 里写 back_view: true）")
        return
    # 全季
    names = set(eps)
    for e in season.get("episodes", []):
        if len(str(e.get("summary", ""))) < 6 or len(str(e.get("hook", ""))) < 4:
            E.append(f"季.json 第 {e.get('ep')} 集缺 summary 或 hook（每集 2–3 句剧情 + 一句结尾钩子）")
        if not all(e.get(k) for k in ("titles", "blurb", "tags")):
            W.append(f"季.json 第 {e.get('ep')} 集还没写发布文案（titles 3 个钩子标题、blurb 80–120 字简介、tags 5–8 个话题），出发布包时要用")
    seq = season.get("episodes", [])
    for key, allowed, zh in (("hook_type", HOOK_TYPES, "同一类钩子"), ("tag", TAGS, "同一个功能标签")):
        vals = [e.get(key) for e in seq]
        for e, v in zip(seq, vals):
            if v is not None and v not in allowed:
                W.append(f"季.json 第 {e.get('ep')} 集 {key}「{v}」要是 {'/'.join(allowed)} 之一（docs/23）")
        for i in range(2, len(vals)):
            if vals[i] and vals[i] == vals[i - 1] == vals[i - 2]:
                W.append(f"季.json 第 {seq[i - 2].get('ep')}–{seq[i].get('ep')} 集连用{zh}「{vals[i]}」：观众会麻木，换一种（docs/22、docs/23 第六节）")
                break
    tg = [e.get("tag") for e in seq]
    for i in range(3, len(tg)):
        if all(x == "压" for x in tg[i - 3:i + 1]):
            W.append(f"季.json 第 {seq[i - 3].get('ep')}–{seq[i].get('ep')} 集连着四集都是「压」：压抑不超过 3 集必须释放（docs/22）")
            break
    for f in season.get("foreshadow", []):
        if f.get("plant") not in names or f.get("payoff") not in names:
            E.append(f"伏笔「{f.get('what')}」的 plant / payoff 集数不在 episodes 里")
        elif eps.index(f["payoff"]) < eps.index(f["plant"]):
            E.append(f"伏笔「{f.get('what')}」回收在埋下之前")
    have = {ep for ep, _ in sr.get("episodes", [])}
    if have and len(have) < len(eps):
        W.append(f"还没写分镜的集：{'、'.join(e for e in eps if e not in have)}（资产文档只包含已写分镜的集）")
    stage = {}
    for c in board.get("clips", []):  # 状态卡只进不退（受伤、疲惫、湿透……），除非写了 state_reset
        for name in c.get("cast", {}):
            ch = chars.get(name, {})
            b, k = ch.get("_base", name), (0 if ch.get("back_view") else ch.get("_stage", 0))
            if ch.get("back_view"):
                continue
            if k < stage.get(b, (0, ""))[0] and not c.get("state_reset"):
                W.append(f"{c['n']}「{name}」退回了更早的状态（之前在 {stage[b][1]} 已经是更后面的状态卡）：状态卡只进不退，确有休息或治疗就写 state_reset")
            if k >= stage.get(b, (0, ""))[0]:
                stage[b] = (k, c["n"])


def bible_char(bible, name):
    return next((c for c in bible.get("characters", []) if c["name"] == name), None)


# ---------------- 类型片库与时长档位（docs/21） ----------------

def _cfg(name):
    return json.load(open(os.path.join(ROOT, "config", name), encoding="utf-8"))


def look_check(bible, board, E, W):
    """设定.json 的 look：{"genre": "G18" 或 ["G18", "G17"], "tier": "1-3"}。没写 look 不检查（老项目）。"""
    look = bible.get("look")
    if not look:
        return
    lib = {c["id"]: c for c in _cfg("genre_library.json")["类型"]}
    tiers = {t["id"]: t for t in _cfg("duration_tiers.json")["档位"]}
    gids = look.get("genre") or []
    gids = [gids] if isinstance(gids, str) else gids
    cards = []
    for g in gids:
        if g not in lib:
            E.append(f"look.genre「{g}」不在类型片库里（python3 -I tools/genre.py --menu）")
        else:
            cards.append(lib[g])
    clips = board.get("clips", [])
    shots = [s for c in clips for s in c.get("shots", [])]
    if cards and shots:
        recs = {r for c in cards for r in c["风格配方"]}
        used = {v.get("recipe") for v in bible.get("worlds", {}).values()} | {v.get("people") for v in bible.get("worlds", {}).values()} | {bible.get("people_recipe")}
        if not used & recs:
            W.append(f"世界和人物的风格配方 {sorted(x for x in used if x)} 都不在所选类型卡的配方 {sorted(recs)} 里：确认是有意混搭，或换配方（docs/21）")
        lo = min(c["剪辑"]["平均镜长"][0] for c in cards)
        hi = max(c["剪辑"]["平均镜长"][1] for c in cards)
        avg = sum(s.get("dur", 0) for s in shots) / len(shots)
        if not lo * 0.75 <= avg <= hi * 1.25:
            W.append(f"全片平均镜长 {avg:.1f} 秒，所选类型的节奏是 {lo}–{hi} 秒（{'；'.join(c['剪辑']['说明'] for c in cards)}）：确认是有意的，或调整镜头时长")
        ban = {m for c in cards for m in c["禁用运镜"]}
        for c in clips:
            for k, s in enumerate(c.get("shots", []), 1):
                if (s.get("move") or {}).get("kind") in ban:
                    W.append(f"第{c.get('n')}条 镜{k} 用了运镜 {s['move']['kind']}，所选类型卡不用它（{'、'.join(sorted(ban))}）")
    tid = look.get("tier")
    if tid:
        if tid not in tiers:
            E.append(f"look.tier「{tid}」不在时长档位里（python3 -I tools/genre.py --tiers）")
            return
        t = tiers[tid]
        total = sum(s.get("dur", 0) for s in shots)
        if not t["秒"][0] <= total <= t["秒"][1]:
            W.append(f"全片 {total:g} 秒，不在档位「{t['名称']}」的 {t['秒'][0]}–{t['秒'][1]} 秒里：换档位，或增删条（扩写见 docs/21 第四节）")
        if not t["条"][0] <= len(clips) <= t["条"][1]:
            W.append(f"全片 {len(clips)} 条，档位「{t['名称']}」建议 {t['条'][0]}–{t['条'][1]} 条")
        nloc = len({c.get("location") for c in clips})
        if nloc > t["场景上限"]:
            W.append(f"用了 {nloc} 个场景，档位「{t['名称']}」建议不超过 {t['场景上限']} 个：场景越多一致性越难保")
        named = [c["name"] for c in bible.get("characters", []) if not c.get("kind")]
        if len(named) > t["角色上限"]:
            W.append(f"有名字的角色 {len(named)} 个，档位「{t['名称']}」建议不超过 {t['角色上限']} 个")


AI_WEAK = [  # Eian 2026-11-05 实测弱点 + docs/14 4.1（提醒，不拦）
    (re.compile(r"镜子|倒影|镜中|镜面"), "镜子和倒影：模型常画错镜像，能删就删，留着就让它只做背景"),
    (re.compile(r"字幕|写着|上面的字|招牌上的|屏幕上的字|文字"), "画面里的文字：模型写不对，改成后期合成"),
    (re.compile(r"交叉|擦肩|穿过人群|绕过对方"), "人物交叉走位：容易穿模或换脸，改成一人静止一人移动"),
    (re.compile(r"由[^，。；、]{1,4}变[^，。；、]{1,4}|熄灭|亮起|灯灭|变暗|变亮"), "一镜内换光：写清光源在画里、只变这一处，其余光不变"),
]
MOVING = re.compile(r"走|跑|进|出|飘|落|推|拉|转|抬|举|放|递|拿|扑|冲|倒|摇|晃|飞|喷|升|滑|流|拖|笑|抖|擦|拍|写|盖|按|撕|踩|抓|攥|松|拔|扫|砸|指|碰|伸|吹|滚|爬|跳|打|摔|翻|开|关|亮|闪|动|移|穿|扔|接|点|眨|张|闭|呼|吸|跪|站起|坐下|靠")
SAME_TIME = re.compile(r"所有人|大家|众人|全体|一起|同时|齐刷刷|纷纷")
ORDER_WORDS = re.compile(r"先|后|接着|随后|最后|依次|一个接一个")
SEQ_WORDS = re.compile(r"然后|接着|随后|又|再")


def ai_weak_check(board, W):
    """AI 视频的固定弱点（docs/14 4.1、Eian 文章）：只提醒，不拦。"""
    for c in board.get("clips", []):
        for k, s in enumerate(c.get("shots", []), 1):
            acts = s.get("action", [])
            acts = acts if isinstance(acts, list) else [acts]
            txt = " ".join(a.get("text", "") if isinstance(a, dict) else str(a) for a in acts)
            st = f"第{c.get('n')}条 镜{k}"
            for rx, why in AI_WEAK:
                m = rx.search(txt)
                if m:
                    W.append(f"{st}「{m.group(0)}」：{why}")
            if s.get("dur", 0) >= 3 and not s.get("move") and s.get("type") not in ("track", "pov") and not s.get("crowd") \
                    and not s.get("line") and not MOVING.search(txt) and not c.get("stillness"):
                W.append(f"{st} 这一镜什么在动？机器、人、焦点、光、前景物至少一样在动，否则画面像一张图片（Eian：画面不能死）；确实要静，在这条写 stillness")
            if SAME_TIME.search(txt) and not ORDER_WORDS.search(txt):
                W.append(f"{st} 多人同时做同一个动作：真人的反应有先后，写清谁先谁后，否则模型会让所有人同一秒动（Eian 实测）")
            if s.get("type") in ("single", "ots", "track"):
                segs = [x for x in re.split(r"[，；。]", txt) if x.strip()]
                if len(segs) >= 6 or len(SEQ_WORDS.findall(txt)) >= 2:
                    W.append(f"{st} 一镜里的动作可能超过两个（{len(segs)} 段）：每镜只写一两个主要动作，其余拆到别的镜头（Eian：一人连做三个动作最容易抽卡）")


# ---------------- 资产 ----------------

def _num(names, idx):
    return "、".join(str(idx[n]) for n in names) if names else "—"


def items(bible, board):
    film = bible["film"]
    uses = {}
    for clip in board.get("clips", []):
        loc = clip["location"]
        tags = [f"@{n}" for n in clip.get("cast", {})] + [f"@prop_{p}" for p in clip.get("props", [])] + [f"@loc_{loc}"]
        if any(s.get("crowd") for s in clip["shots"]):
            tags.append("@crowd_" + next(l["world"] for l in bible["locations"] if l["name"] == loc))
        for t in tags:
            uses.setdefault(t, []).append(clip["n"])
    use = lambda t: "第 " + "、".join(uses.get(t, [])) + " 条" if uses.get(t) else "备用"  # noqa: E731
    out, idx = [], {}

    def push(key, it):
        out.append(it)
        idx[key] = len(out)

    for c in bible["characters"]:
        if c.get("kind") == "voice":  # 只有声音：不出图
            continue
        if c.get("kind") == "giant":  # 巨物：自然姿态设定图，不做三视图和脸模糊（docs/19）
            push(c["name"], dict(tag=f"@{c['name']}", world="__giant__" + c["name"], file=f"{c['name']}_设定图.png", type="巨物设定图", rec=REC, deps="—",
                                 art=f"剪影识别点：{c['silhouette']}" if c.get("silhouette") else "",
                                 brief_en=c["brief_en"], voice_en=c.get("voice_en", ""),
                                 use=use(f"@{c['name']}"), lock=c["lock"],
                                 en=f"Create a creature / machine design sheet for a {film}: {c['brief_en']}",
                                 must=f"MUST BE READABLE: {c['must_en']} CLOSE-UP: {c.get('portrait_en', 'its most recognisable part.')}",
                                 check=c.get("check", []) + ["自然姿态的三分之四正面 + 侧面 + 局部特写", "没有人", "没有文字"],
                                 duty=f"「{c['name']}」：外形、材质、颜色和比例从设定图取；不继承灰底和设定图版式。"))
            continue
        must = f"MUST BE READABLE: {c['must_en']} PORTRAIT: {c.get('portrait_en', 'restrained expression, mouth closed.')}"
        push(c["name"], dict(tag=f"@{c['name']}", world="__people__", file=f"{c['name']}_三视图.png", type="人物三视图", rec=REC, deps="—",
                             art=f"剪影识别点：{c['silhouette']}" if c.get("silhouette") else "",
                             brief_en=c["brief_en"], voice_en=c.get("voice_en", ""),
                             use=use(f"@{c['name']}"), lock=c["lock"],
                             en=f"Create a character reference sheet for a {film}: {c['brief_en']}", must=must,
                             check=c.get("check", []) + ["正面全身像的脸模糊成椭圆，右侧大头像清楚", "没有文字"],
                             duty=f"「{c['name']}」：脸只从右侧大头像取；体型和服装从左侧全身像取；正面全身像的模糊椭圆不是脸；不继承灰底和三视图版式。"))
        for v in c.get("variants", []):
            push(v["name"], dict(tag=f"@{v['name']}", world="__people__", file=f"{v['name']}_三视图.png", type="状态变体", rec=REC,
                                 brief_en=v.get("brief_en", c["brief_en"]), voice_en=c.get("voice_en", ""),
                                 deps=str(idx[c["name"]]), use=use(f"@{v['name']}"), lock=v["lock"],
                                 en=f"Edit Image 1, the character sheet of {c['brief_en']}. Keep the face, body, layout and background exactly the same.",
                                 must=f"Change only: {v['edit_en']}", check=v.get("check", []) + ["脸和版式不变"],
                                 duty=f"「{c['name']}」的另一个状态（{v['name']}）：脸只从右侧大头像取；不继承灰底和三视图版式。"))
    for p in bible.get("props", []):
        dep = [p["ref"]] if p.get("ref") in idx else []
        ref = "The prop must match the one carried by the character in Image 1. " if dep else ""
        push("prop:" + p["name"], dict(tag=f"@prop_{p['name']}", world="__people__", file=f"道具_{p['name']}.png", type="道具", rec=REC, brief_en=p["en"],
                                       deps=_num(dep, idx), use=use(f"@prop_{p['name']}"), lock=p["lock"],
                                       en=f"A prop reference for a {film}, on a seamless neutral grey background, three-quarter view: {p['en']} {ref}".strip(),
                                       must=p["must_en"] + " No text, symbols or labels.", check=p.get("check", []) + ["没有文字"],
                                       duty=f"「{p['name']}」：只取外形、材质和状态；不继承灰底。"))
    for l in bible["locations"]:
        hv = l.get("human_view")
        anchors = "、".join(l.get("anchor", []))
        art = f"锚点物：{anchors}；光逻辑：{l.get('light', '')}；时代：{l.get('era', '')}；使用痕迹：{l.get('wear', '')}" + (f"；画面对冲：{l['contrast']}" if l.get('contrast') else "")
        push("loc:" + l["name"], dict(tag=f"@loc_{l['name']}", world=l["world"], file=f"场景_{l['name']}.png", brief_en=l["en"], art=art,
                                      geo_note=f"「{l['name']}」美术：{art}。",
                                      type="人类视角机位" if hv else "场景主图", rec=REC, deps="—", use=use(f"@loc_{l['name']}"), lock=l["lock"],
                                      en=f"A location reference image for a {film}, no characters: {l['en']}",
                                      must=l["must_en"] + " The scene is completely uninhabited. Full-frame image with no black bars. No readable text or signs." +
                                      (" No giant creatures, robots or machines anywhere in the scene, not even in the far distance or as silhouettes." if any(c.get("kind") == "giant" for c in bible["characters"]) else ""),
                                      check=l.get("check", []) + [f"锚点物都在：{anchors}", "光的方向和冷暖符合光逻辑", "没有人", "没有黑边", "没有文字"],
                                      duty=f"「{l['name']}」：只取空间结构、材质和光；不作首帧，不继承构图。"))
        if l.get("grid"):
            push("grid:" + l["name"], dict(tag=f"@loc_{l['name']}_grid", world=l["world"], file=f"场景_{l['name']}_宫格.png", type="场景宫格", rec=REC,
                                           brief_en="a 2x2 multi-view sheet of " + l["en"],
                                           deps=str(idx["loc:" + l["name"]]), use=use(f"@loc_{l['name']}"), lock=l["lock"],
                                           en="Image 1 is the only visual source. Rebuild the same place in 3D and render one 2x2 four-panel multi-view sheet, 16:9, no people, no text.",
                                           must=l.get("grid_en", "Panel 1: overhead plan. Panel 2: eye-level main view. Panel 3: reverse view. Panel 4: side view. Same materials and light in every panel."),
                                           check=["四格是同一个空间", "没有人、没有字"],
                                           duty=f"「{l['name']}」多视角宫格：保持同一空间的布局和光；不作首帧，不继承宫格版式。"))
    for c in bible.get("crowds", []):
        refs = [n for n in c.get("refs", [])]
        refs_en = (f"Images 1–{len(refs)} are the main characters." if len(refs) > 1 else "Image 1 is a main character.") if refs else ""
        it = K.crowd_item(c["world"], [tuple(r) for r in c["roster"]], f"@crowd_{c['world']}", f"群像_{c['world']}.png", _num(refs, idx),
                          use(f"@crowd_{c['world']}"), c["world"], refs_en, c["world_en"], film)
        it["brief_en"] = c["world_en"]
        push("crowd:" + c["world"], it)
    return out


CAMS = {c["id"]: c for c in json.load(open(os.path.join(ROOT, "config", "camera_presets.json"), encoding="utf-8"))["预设"]}


def cam_preset(bible, world=None, shot=None):
    """成像逻辑（4.0 alpha.14）：这一镜用哪组摄影机预设。单镜 camera > 世界 camera > 全片 film_look.camera；都没写返回 None。"""
    cid = (shot or {}).get("camera") or (bible.get("worlds", {}).get(world, {}) if world else {}).get("camera") or (bible.get("film_look") or {}).get("camera")
    return CAMS.get(cid) if cid else None


def cam_line(c, lang="en", stills=False):
    if lang == "en":
        return f"shot on {c['camera']} with {c['lens']} lenses" + (f" on {c['stock']}" if c.get("stock") else "") + f"; {c['stills_en' if stills else 'effects_en']}"
    return f"摄影机：{c['camera']} + {c['lens']}" + (f" + {c['stock']}" if c.get("stock") else "") + f"；成像：{c['effects_zh']}"


def with_look(en, bible, world=None):
    """全片影调（设定 film_look）接在世界配方后面；有了影调就去掉配方里的"干净无噪点"（4.0 alpha.14）。人物三视图、道具图不加，保持中性参考。"""
    fl = bible.get("film_look")
    if not fl:
        return en
    en = re.sub(r"[;,]?\s*clean, noise-free image", "", en).rstrip(" ;,")
    c = cam_preset(bible, world)
    return f"{en}" + (f"; {fl['en']}" if fl.get("en") else "") + (f"; {cam_line(c, stills=True)}" if c else "")


def style_map(bible):
    rec = {r["id"]: r for r in json.load(open(os.path.join(ROOT, "config", "style_recipes.json"), encoding="utf-8"))["配方"]}
    en = {w: with_look(rec[v["recipe"]]["en"], bible, w) for w, v in bible["worlds"].items()}
    en["__people__"] = rec[bible["people_recipe"]]["en"]
    for c in bible["characters"]:
        if c.get("kind") == "giant":
            en["__giant__" + c["name"]] = rec[c.get("recipe", bible.get("giant_recipe", "R15"))]["en"]
    zh = "；".join(f"{w}（{v['recipe']}）：{rec[v['recipe']]['zh']}" for w, v in bible["worlds"].items())
    zh += f"；人物（{bible['people_recipe']}）：{rec[bible['people_recipe']]['zh']}"
    note = "；".join(f"{w} {v['recipe']} {rec[v['recipe']]['名称']}" for w, v in bible["worlds"].items()) + \
        f"；人物与道具 {bible['people_recipe']} {rec[bible['people_recipe']]['名称']}"
    return en, "质感：" + zh + "。", note


# ---------------- 镜头 ----------------

def _v(a, b):
    return [b[0] - a[0], b[1] - a[1], b[2] - a[2]]


def _lerp(p, q, f):
    return [p[i] + (q[i] - p[i]) * f for i in range(3)]


def golden(cam, look, lens, face, ratio=0.12, hw=18.0):
    """挪注视点，让主体落在画面约 38% / 62%（Eian：每一秒截图都要好看）。
    主体朝画面哪边看，就把他放在另一边，前面留出视线空间。"""
    v = [look[i] - cam[i] for i in range(3)]
    d = math.hypot(v[0], v[1]) or 1.0
    right = [v[1] / d, -v[0] / d, 0.0]  # 画面向右的方向
    sgn = 1.0 if face[0] * right[0] + face[1] * right[1] >= 0 else -1.0  # 朝右看 → 注视点右移 → 人落在左边 38%
    shift = sgn * d * 2 * ratio * hw / lens  # tan(水平半视场角) = 半画幅宽 / 焦距；横屏 18 mm，竖屏 18 × 9/16
    return [look[0] + right[0] * shift, look[1] + right[1] * shift, look[2]]


class Clip:
    def __init__(self, bible, clip, li):
        self.b, self.c = bible, clip
        self.locs = {l["name"]: l for l in bible["locations"]}
        self.chars = char_map(bible)
        self.loc = self.locs[clip["location"]]
        self.origin = [li * 1000.0, 0.0, 0.0]
        self.marks = {k: K.add(self.origin, v) for k, v in self.loc["stage"]["marks"].items()}
        self.vertical = bible.get("ratio") == "9:16"
        self.hw = 18.0 * 9 / 16 if self.vertical else 18.0  # 水平半画幅（mm）：竖屏时传感器长边是竖的

    def pt(self, x):
        if isinstance(x, str):
            if x in self.marks:
                return self.marks[x]
            if x in self.c.get("cast", {}):
                return self.pos(x, 0)
            raise Bad(f"找不到点「{x}」")
        return K.add(self.origin, x)

    def path(self, name):
        cv = self.c["cast"][name]
        pts = cv["path"] if "path" in cv else [dict(cv, t=0)]
        return pts

    DROP = {"sit": 0.24, "sit_ground": 0.46, "kneel": 0.25, "kowtow": 0.55, "crouch": 0.22, "bend": 0.2, "pick": 0.38,
            "duck": 0.38, "lie": 0.85, "slump": 0.55}

    def drop(self, name, t):
        """节拍里的坐、跪、蹲、弯腰让头变低：机位按变低的头算（4.0 alpha.8）。"""
        best = 0.0
        for b in self.beats():
            if b.get("who") == name and b.get("do") in self.DROP:
                t1 = b["_t"] + b["dur"] if b.get("dur") else (10 ** 6 if b.get("hold") else b["_end"])
                if b["_t"] <= t + 0.4 <= t1 + 0.4:
                    best = max(best, self.DROP[b["do"]])
        return best * (self.chars[name]["height"] if name in self.chars else 1.7)

    def pos(self, name, t):
        p = self._pos(name, t)
        dz = self.drop(name, t) if name in self.chars and "beats" in str(self.c.get("shots")) else 0.0
        return [p[0], p[1], p[2] - dz] if dz else p

    def _look_name(self, at):
        if isinstance(at, list):
            return None
        return at[5:] if at.startswith("prop_") else at

    def cam_move(self, s):
        """手写机位的运动怎么写进提示词：挂在车上 / 人身上跟着走、车内往外看、起止两点之间移动。没有运动返回 None。"""
        c = s.get("cam") or {}
        if s.get("type") == "pov":
            return {"zh": "镜头跟着主观者的头一起走动", "en": "the camera moves with the viewer's head, first-person"}
        if c.get("mount"):
            m = c["mount"]
            nm = m[5:] if m.startswith("prop_") else m
            if m.startswith("prop_"):
                pr = next((x for x in self.b.get("props", []) if x["name"] == nm), {})
                sz, off = pr.get("size", [0.3, 0.2, 0.1]), c.get("offset", [0, 0, 0])
                if all(abs(off[i]) < sz[i] / 2 for i in range(3)):
                    return {"zh": f"镜头在{nm}里往前看，跟着车一起移动", "en": "the camera looks out from inside the vehicle, travelling with it"}
                return {"zh": f"镜头挂在{nm}外侧，跟着它一起高速移动", "en": "the camera is mounted on the vehicle and travels with it"}
            return {"zh": f"镜头跟在{nm}身边一起移动", "en": "the camera travels alongside the subject, following closely"}
        if c.get("end"):
            lk = c.get("look")
            on = (lk[5:] if lk.startswith("prop_") else lk) if isinstance(lk, str) else None
            return {"zh": "镜头平稳移动" + (f"，始终对着{on}" if on else ""), "en": "the camera glides smoothly" + (", staying on the subject" if on else "")}
        return None

    def mount_point(self, m, t):
        """挂载点在 t 秒的位置：'prop_红车' 取车身中心，人名取头。"""
        if m.startswith("prop_"):
            pr = next((x for x in self.b.get("props", []) if x["name"] == m[5:]), {})
            q = self.prop_pos(m[5:], t)
            return [q[0], q[1], q[2] + pr.get("size", [0.3, 0.2, 0.1])[2] / 2]
        return K.head(self.pos(m, t), self.height(m))

    def look_keys(self, eye, looks, t0):
        """视线关键帧：[{t: 镜内秒, at: 站位点 / 人名 / prop_道具 / [x,y,z]}] → [{t: 整条秒, yaw, pitch}]（yaw 和人的 face 同一套角度）。"""
        out = []
        for L in looks:
            tt = t0 + L.get("t", 0)
            e = eye(tt)
            at = L["at"]
            if isinstance(at, str) and at in self.c.get("cast", {}):
                tg = K.head(self.pos(at, tt), self.height(at))
            elif isinstance(at, str) and at.startswith("prop_"):
                tg = self.mount_point(at, tt)
            else:
                tg = self.pt(at)
            hz = math.hypot(tg[0] - e[0], tg[1] - e[1]) or 1e-6
            out.append({"t": round(tt, 3), "yaw": round(K.face_to(e, tg), 2), "pitch": round(math.degrees(math.atan2(tg[2] - e[2], hz)), 2)})
        return out

    @staticmethod
    def _yaw_point(p, lk, d=10.0):
        a, b = math.radians(lk["yaw"]), math.radians(lk["pitch"])
        return [round(p[0] + math.sin(a) * d, 4), round(p[1] - math.cos(a) * d, 4), round(p[2] + math.tan(b) * d, 4)]

    def mounted_cam(self, s, t0, c):
        """cam.mount："prop_红车" / 人名；offset：相对挂载点的偏移（米，场景坐标）；look 或 looks：看哪里。
        镜头在车身里面时（车内镜头），白模把这辆车藏起来，换成一块引擎盖，从车里往外看不会整屏一块颜色。"""
        off = c.get("offset", [0, 0, 0])
        eye = lambda tt: [a + b for a, b in zip(self.mount_point(c["mount"], tt), off)]  # noqa: E731
        lks = self.look_keys(eye, c.get("looks") or [{"t": 0, "at": c["look"]}], t0)
        p = eye(t0)
        cm = K.cam(t0, c.get("lens", 35), p, self._yaw_point(p, lks[0]))
        cm.update(mount=c["mount"], mount_off=off, looks=lks, hide=list(c.get("hide", [])))
        if c.get("bob"):
            cm["bob"] = c["bob"]
        if c["mount"].startswith("prop_"):
            pr = next((x for x in self.b.get("props", []) if x["name"] == c["mount"][5:]), {})
            sz = pr.get("size", [0.3, 0.2, 0.1])
            if all(abs(off[i]) < sz[i] / 2 for i in range(3)):
                cm["hide"].append(c["mount"])
                for n2, cv2 in self.c.get("cast", {}).items():  # 镜头就在这个座位上：坐在这里的人也藏起来（司机的主观）
                    if cv2.get("ride") == c["mount"][5:] and math.dist(self.seat(n2)[:2], off[:2]) < 0.45:
                        cm["hide"].append(n2)
                a = math.radians(lks[0]["yaw"])
                fwd = [math.sin(a), -math.cos(a)]
                cm["hood"] = {"size": [sz[0] * 0.92, 1.5, 0.05], "off": [fwd[0] * 1.25, fwd[1] * 1.25, -0.42], "yaw": lks[0]["yaw"],
                              "color": BOXCOLOR.get(pr.get("proxy_color", "white"), BOXCOLOR["white"])}
        return cm

    def prop_pos(self, name, t):
        """道具在 t 秒的位置（prop_states 的 at / path），载具和坐在里面的人、挂在车上的镜头都用它。"""
        st = self.c.get("prop_states", {}).get(name, {})
        pts = st.get("path") or [{"t": 0, "at": st.get("at")}]
        prev = pts[0]
        for q in pts:
            tq = q["t"]
            if tq <= t:
                prev = q
            else:
                f = (t - prev["t"]) / (tq - prev["t"]) if tq > prev["t"] else 1.0
                return _lerp(self.pt(prev["at"]), self.pt(q["at"]), f)
        return self.pt(prev["at"])

    def seat(self, name):
        cv = self.c["cast"][name]
        return cv.get("seat", [-0.4, 0.2, 0.1])  # 默认驾驶座：车身中心偏左、偏前，坐下后头在车顶下面

    def _pos(self, name, t):
        cv = self.c["cast"].get(name, {})
        if cv.get("ride"):  # 坐在车里的人：车在哪人在哪
            q = self.prop_pos(cv["ride"], t)
            return [q[i] + self.seat(name)[i] for i in range(3)]
        pts = self.path(name)
        prev = pts[0]
        for p in pts:
            if p["t"] <= t:
                prev = p
            else:
                f = (t - prev["t"]) / (p["t"] - prev["t"])
                return _lerp(self.pt(prev["at"]), self.pt(p["at"]), f)
        return self.pt(prev["at"])

    def face_dir(self, name, t):
        cv = self.c["cast"][name]
        f = None
        for p in self.path(name):
            if p["t"] <= t:
                f = p.get("face", cv.get("face"))
        pos = self.pos(name, t)
        if isinstance(f, (int, float)):
            ang = math.radians(f)
        elif f:
            tg = self.pos(f, t) if f in self.c["cast"] else self.pt(f)
            ang = math.radians(K.face_to(pos, tg))
        else:
            ang = 0.0
        return [math.sin(ang), -math.cos(ang), 0.0]

    def height(self, name):
        return self.chars[name]["height"]

    def people(self):
        out = []
        for i, name in enumerate(self.c["cast"]):
            if self.c["cast"][name].get("hidden"):  # 藏在轿里、箱里、门后的人：白模不渲，模型就不会把代理画出来（4.0 alpha.10，实拍《好日子》第 6 条）
                continue
            keys = []
            for p in ([] if self.c["cast"][name].get("ride") else self.path(name)):
                f = p.get("face", self.c["cast"][name].get("face", 0))
                face = f if isinstance(f, (int, float)) else (self.pos(f, p["t"]) if f in self.c["cast"] else self.pt(f))
                keys.append((p["t"], self.pt(p["at"]), face, p.get("lean", 0)))
            dur = sum(s["dur"] for s in self.c["shots"])
            if not keys:  # 坐车的人：位置是相对车的座位（白模里用约束挂在车上）
                f = self.c["cast"][name].get("face", 180)
                keys = [(0.0, self.seat(name), f if isinstance(f, (int, float)) else 180, 0)]
            keys.append((dur, keys[-1][1], keys[-1][2], keys[-1][3]))
            ci = [c["name"] for c in self.b["characters"] if c.get("kind") != "voice"].index(self.chars[name].get("_base", name))  # 和投喂包里的识别色说明同一套编号
            per = K.person(name, PALETTE[ci % len(PALETTE)], self.height(name), keys)
            if self.c["cast"][name].get("ride"):  # 坐在车里：白模把人挂在车上（座位偏移），速度检查不查
                per["ride"] = "prop_" + self.c["cast"][name]["ride"]
                per["seat"] = self.seat(name)
            if self.chars[name].get("kind") == "giant":
                per["shape"] = "giant"  # 白模用巨物形状代理（树干、根须、空心头），不是放大的小人
                for k, p in zip(per["keys"], self.path(name)):  # head_at：巨物俯身，空心头落到这个站位点（cast 或路径点上写）
                    ha = p.get("head_at", self.c["cast"][name].get("head_at"))
                    if ha:
                        k["head"] = self.pt(ha)
                if per["keys"][-2].get("head"):
                    per["keys"][-1]["head"] = per["keys"][-2]["head"]
            poses = [b for b in self.beat_poses() if b["who"] == name]
            if self.c["cast"][name].get("ride") and not any(b["pose"] in ("sit", "stand") for b in poses):  # 坐在车里：默认整条坐着，头不会从车顶冒出来
                poses = [{"who": name, "t0": 0.0, "t1": dur, "pose": "sit"}] + poses
            if poses:
                per["poses"] = [{k: v for k, v in b.items() if k != "who"} for b in poses]
            out.append(per)
        return out + self.extras()

    def light_previs(self):
        """白模的光型：场景写了 previs_light 用它（"sunset" 或 {"preset": "sunset", "from": [1, -0.3]}），
        否则从光逻辑的字里认：夜 / 黄昏后的天光 / 落日斜光 / 正午顶光 / 清晨 / 阴天。认不出就不打光（Workbench 平光）。"""
        lp = self.loc.get("previs_light")
        if lp:
            return {"preset": lp} if isinstance(lp, str) else dict(lp)
        txt = self.loc.get("light", "")
        for preset, words in (("night", ("夜", "月光")), ("dusk", ("落日后", "黄昏", "暮色", "傍晚", "日落后")),
                              ("sunset", ("落日", "夕阳", "斜阳", "日落", "晚霞")), ("noon", ("正午", "顶光", "当头", "烈日", "午后")),
                              ("dawn", ("晨", "清早", "日出", "朝阳")), ("overcast", ("阴天", "阴云", "雨"))):
            if any(w in txt for w in words):
                return {"preset": preset}
        return None

    # ---------- 精准白模（4.0 alpha.8）：节拍、群演、道具 ----------
    def _target(self, tg, do="reach"):
        """节拍里的 target：人名（看 → 头，其余 → 右手）、人名.头 / 人名.右手 / 人名.左手、站位点名、道具名、[x,y,z]。"""
        if tg is None:
            return None
        if isinstance(tg, list):
            return self.pt(tg)
        who, _, part = tg.partition(".")
        names = set(self.c.get("cast", {})) | {f"群演{i}" for g in self.c.get("extras", []) for i in g.get("ids", [])}
        if who in names:
            part = part or ("头" if do in ("look", "nod", "point") else "右手")
            return f"{who}:" + {"头": "head", "右手": "handR", "左手": "handL", "手": "handR"}.get(part, "head")
        if tg in self.marks:
            return self.marks[tg]
        if tg in {p["name"] for p in self.b.get("props", [])}:
            return "prop_" + tg
        raise Bad(f"节拍的目标「{tg}」既不是人、站位点，也不是道具")

    def beats(self):
        t0, out = 0.0, []
        for s in self.c["shots"]:
            for b in s.get("beats", []):
                out.append(dict(b, _t=round(t0 + b["t"], 3), _end=round(t0 + s["dur"], 3)))
            t0 += s["dur"]
        return out

    def beat_poses(self):
        out = []
        for b in self.beats():
            if not b.get("who") or not b.get("do"):
                continue
            t1 = b["_t"] + b["dur"] if b.get("dur") else b["_end"]
            if b.get("hold"):
                t1 = sum(s["dur"] for s in self.c["shots"])
            item = {"who": b["who"], "t0": b["_t"], "t1": round(t1, 3), "pose": b["do"]}
            if b.get("target") is not None:
                item["target"] = self._target(b["target"], b["do"])
            if b.get("look") is not None:
                item["look"] = self._target(b["look"], "look")
            if b.get("arm"):
                item["arm"] = b["arm"]
            out.append(item)
        merged = []  # 同一个人、同一个姿态、同一个目标的几段重叠时合成一段：白模里姿态是叠加的，重叠两次幅度就翻倍（4.0 alpha.12：连写三个 hold 的"双手在前"，村长的胳膊举过了头顶）
        for it in sorted(out, key=lambda x: (x["who"], x["pose"], json.dumps(x.get("target")), x.get("arm", ""), x["t0"])):
            m = merged[-1] if merged else None
            if m and all(m.get(k) == it.get(k) for k in ("who", "pose", "target", "arm", "look")) and it["t0"] <= m["t1"] + 0.5:  # 相隔不到 0.5 秒也接上，免得一收一放的过渡里幅度忽高忽低
                m["t1"] = max(m["t1"], it["t1"])
            else:
                merged.append(dict(it))
        return sorted(merged, key=lambda x: x["t0"])

    def extras(self):
        """群演进白模：clip 的 extras 是一组组群演，按队形站位、可以沿路径走、按时间换姿态。"""
        groups = self.c.get("extras", [])
        if not groups:
            return []
        dur = sum(s["dur"] for s in self.c["shots"])
        out = []
        for gi, g in enumerate(groups):
            world = g.get("world", self.loc["world"])
            roster = next((c["roster"] for c in self.b.get("crowds", []) if c["world"] == world), [])
            ids = g["ids"]
            n = len(ids)
            sp = g.get("spacing", 0.9)
            path = g.get("path") or [{"t": 0, "at": g["at"]}]

            def anchor(t, path=path):
                prev = path[0]
                for q in path:
                    if q["t"] <= t:
                        prev = q
                    else:
                        f = (t - prev["t"]) / (q["t"] - prev["t"])
                        return _lerp(self.pt(prev["at"]), self.pt(q["at"]), f)
                return self.pt(prev["at"])
            times = sorted({0.0, dur} | {q["t"] for q in path} | {x for k in ("t0", "t1") for x in [g.get(k)] if x is not None})
            dup = {q["t"] for q in path if sum(1 for x in path if abs(x["t"] - q["t"]) < 1e-9) > 1}
            times = sorted(set(times) | {round(t - 0.05, 3) for t in dup if t > 0.05})  # 同一时刻两个位置 = 切点上换了地方：前一帧还在原处，不要慢慢走过去
            pose_list = g.get("poses", [])
            for i, cid in enumerate(ids):
                name = f"群演{cid}"
                hm = re.search(r"(\d{3}) cm", roster[cid - 1][1]) if 0 < cid <= len(roster) else None
                h = int(hm.group(1)) / 100 if hm else 1.68
                keys = []
                last = None  # 停下来时保持最后走的方向，不要突然转回默认朝向
                last_rt = None
                for t in times:
                    a = anchor(t)
                    ft = g.get("face") if t <= g.get("face_until", 1e9) else None  # face_until 之后朝走的方向（跪拜完转身下山）
                    fp = (self.pos(ft, t) if isinstance(ft, str) and ft in self.c.get("cast", {}) else self.pt(ft)) if ft else None  # face 可以是人名、站位点或坐标
                    if fp is None and len(path) > 1:
                        nxt = anchor(min(dur, t + 0.5))
                        if math.dist(nxt[:2], a[:2]) > 1e-3:
                            fp = nxt
                        elif last:
                            fp = [a[0] + last[0], a[1] + last[1], a[2]]
                        else:  # 开场站着、过一会儿才走：先朝着要走的方向，起步时队形不会整个转一圈
                            fut = next((anchor(q["t"]) for q in path if q["t"] > t and math.dist(anchor(q["t"])[:2], a[:2]) > 1e-3), None)
                            if fut:
                                fp = fut
                    fp = fp or [a[0], a[1] - 1, a[2]]
                    fwd = [fp[0] - a[0], fp[1] - a[1]]
                    L = math.hypot(*fwd) or 1.0
                    fwd = [fwd[0] / L, fwd[1] / L]
                    last = fwd
                    rt = [fwd[1], -fwd[0]]
                    if last_rt and rt[0] * last_rt[0] + rt[1] * last_rt[1] < 0:  # 掉头走：左右不跟着镜像，否则两个人会从中间那个人身上穿过去（《好日子》第 6 条崖边）
                        rt = [-rt[0], -rt[1]]
                    last_rt = rt
                    if g.get("ahead") and t <= g.get("ahead_until", 1e9) + 1e-6:  # 抬轿、抬箱：前一组在道具前面 ahead 米，后一组写负数，人不站进道具里（《好日子》第 6 条）
                        a = [a[0] + fwd[0] * g["ahead"], a[1] + fwd[1] * g["ahead"], a[2]]
                        if fp is not None and not ft:
                            fp = [fp[0] + fwd[0] * g["ahead"], fp[1] + fwd[1] * g["ahead"], fp[2]]
                    kind = g.get("formation", "line")
                    if kind in ("line", "rows", "column"):
                        cols = 1 if kind == "column" else (g.get("cols", n) if kind == "rows" else n)
                        r, c = divmod(i, cols)
                        lat = (c - (cols - 1) / 2) * sp
                        back = r * sp * (1.4 if kind == "rows" else 1.0)
                        pos = [a[0] + rt[0] * lat - fwd[0] * back, a[1] + rt[1] * lat - fwd[1] * back, a[2]]
                        face = fp if kind != "column" else [pos[0] + fwd[0], pos[1] + fwd[1], pos[2]]
                    else:
                        rad = g.get("radius", 2.0)
                        if g.get("radius_to") is not None and g.get("t1") is not None:
                            u = min(1.0, max(0.0, (t - g.get("t0", 0)) / max(0.01, g["t1"] - g.get("t0", 0))))
                            rad += (g["radius_to"] - rad) * min(1.0, max(0.0, u * n / max(1, n - 1) - i / max(1, n)))  # 一个接一个往外退
                        ang = 2 * math.pi * i / n + g.get("angle0", 0)
                        pos = [a[0] + rad * math.cos(ang), a[1] + rad * math.sin(ang), a[2]]
                        face = a if kind == "ring" else [2 * pos[0] - a[0], 2 * pos[1] - a[1], pos[2]]
                    keys.append((t, pos, face, 0))
                tint = 0.72 + 0.04 * (cid % 4)
                per = K.person(name, [tint, tint * 0.96, tint * 0.86], h, keys)
                poses = [dict(t0=round(q["t0"] + i * q.get("stagger", 0), 3), t1=q["t1"], pose=q["pose"]) for q in pose_list]
                poses += [{k: v for k, v in b.items() if k != "who"} for b in self.beat_poses() if b["who"] == name]
                if poses:
                    per["poses"] = poses
                out.append(per)
        return out

    def effects_previs(self):
        """clip 的 effects：[{kind: debris|split|fall|reach, t0, t1, at: 站位点或坐标, to?, color?: BOXCOLOR 名, …}] → 白模特效代理。"""
        out = []
        for e in self.c.get("effects", []):
            q = dict(e)
            q["at"] = self.pt(e["at"])
            if e.get("to"):
                q["to"] = self.pt(e["to"])
            q["color"] = BOXCOLOR.get(e.get("color", "stone"), BOXCOLOR["stone"]) if isinstance(e.get("color", "stone"), str) else e["color"]
            out.append(q)
        return out

    def props_previs(self):
        """会动的道具：clip 的 prop_states 给初始位置（站位点名，或"人名.右手"拿在手里），节拍里 prop + do 让它被拿、放、移、摆、出现、消失。"""
        states = self.c.get("prop_states", {})
        pb = [b for b in self.beats() if b.get("prop")]
        names = list(dict.fromkeys(list(states) + [b["prop"] for b in pb]))
        props = {p["name"]: p for p in self.b.get("props", [])}
        out = []
        for nm in names:
            pr = props[nm]
            st = states.get(nm, {})
            spec = {"name": nm, "size": pr.get("size", [0.3, 0.2, 0.1]), "color": BOXCOLOR.get(pr.get("proxy_color", "white"), BOXCOLOR["white"]), "keys": []}
            if pr.get("vehicle"):  # 载具：速度按车查（docs/19）
                spec["vehicle"] = True
            for k in ("open", "open_at", "tear_from"):
                if st.get(k) is not None:
                    spec[k] = st[k]
            at = st.get("at")
            for q in st.get("path", []):  # 沿路径走（被抬着的花轿、被开走的车）
                spec["keys"].append({"t": q["t"], "at": self.pt(q["at"])})
            if st.get("path") and not at:
                at = st["path"][0]["at"]
            if at and "." in at:
                spec["at"] = [0, 0, -50]
                spec["keys"].append({"t": 0, "attach": self._target(at, "hold")})
            else:
                spec["at"] = self.pt(at) if at else [0, 0, -50]
            held = at if at and "." in at else None  # 现在拿在谁手里（"人名.右手"），move 之前要先松开

            def near(tg, t):  # 被拿着的道具大概在哪：头顶或手的位置
                who, _, part = tg.partition(".")
                p0 = self.pos(who, t)
                h = self.chars[who]["height"] if who in self.chars else 1.7
                return [p0[0], p0[1], p0[2] + (0.9 * h if part == "头" else 0.55 * h)]
            for b in sorted((b for b in pb if b["prop"] == nm), key=lambda b: b["_t"]):
                t, do = b["_t"], b["do"]
                if do == "take":
                    spec["keys"].append({"t": t, "attach": self._target(b["to"], "hold")})
                    held = b["to"]
                elif do == "put":
                    spec["keys"].append({"t": t, "detach": self.pt(b["to"])})
                    held = None
                elif do == "move":
                    start = near(held, t) if held else (spec["keys"][-1].get("at") or spec["keys"][-1].get("detach") if spec["keys"] else spec["at"]) or spec["at"]
                    spec["keys"].append({"t": t, "detach": start} if held else {"t": t, "at": start})
                    spec["keys"].append({"t": round(t + b.get("dur", 1.0), 3), "at": self.pt(b["to"])})
                    held = None
                elif do == "swing":
                    spec.setdefault("swing", []).append({"t0": t, "t1": round(t + b.get("dur", 2.0), 3), "amp": b.get("amp", 15), "hz": b.get("hz", 1.0)})
                elif do in ("show", "hide"):
                    spec.setdefault("_vis", []).append((t, do))
            if spec.get("_vis"):
                vis, on = [], 0.0 if not any(d == "show" for _, d in spec["_vis"]) else None
                for t, d in spec.pop("_vis"):
                    if d == "show":
                        on = t
                    elif on is not None:
                        vis.append([on, t])
                        on = None
                if on is not None:
                    vis.append([on, sum(s["dur"] for s in self.c["shots"])])
                spec["visible"] = vis
            out.append(spec)
        return out

    def boxes(self):
        st = self.loc["stage"]
        w, d = st.get("size", [20, 20])
        out = [K.bo(f"{self.loc['name']}_Floor", self.origin, [-w / 2, -d / 2, -0.1], [w / 2, d / 2, 0], BOXCOLOR["floor"])]
        for bx in st.get("boxes", []):
            out.append(K.bo(f"{self.loc['name']}_{bx['name']}", self.origin, bx["min"], bx["max"], BOXCOLOR.get(bx.get("color", "white"), BOXCOLOR["white"])))
        return out

    def camera(self, s, t0):
        cm = self._camera(s, t0)
        ang = s.get("angle")
        if ang and not s.get("cam") and isinstance(cm.get("look_at"), list) and cm.get("keys"):  # 机位高低：不全在人眼高度（4.0 alpha.14）
            lz = cm["look_at"][2]
            z = {"ground": self.origin[2] + 0.25, "low": max(self.origin[2] + 0.35, lz - 0.75), "high": lz + 1.6, "overhead": lz + 4.0}.get(ang)
            if z is not None:
                for k in cm["keys"]:
                    k["pos"] = [k["pos"][0], k["pos"][1], round(z, 4)]
        mv = s.get("move")
        if not mv or mv.get("kind") not in MOVES or len(cm["keys"]) > 1:
            return cm
        if mv["kind"] == "handheld":
            cm["handheld"] = 0.015
            return cm
        cm["keys"].append({"t": t0 + s["dur"], "pos": self.move_end(cm["keys"][0]["pos"], cm["look_at"], mv)})
        if mv.get("speed") != "steady":  # 推拉摇升降：起步和收尾缓一点，像真的轨道和摇臂（匀速的不缓）
            cm["ease"] = True
        return cm

    @staticmethod
    def move_end(p, lk, mv):
        kind = mv["kind"]
        if kind == "handheld":
            return list(p)
        v = _v(p, lk)
        L = math.hypot(v[0], v[1]) or 1.0
        f, side = [v[0] / L, v[1] / L, 0.0], [-v[1] / L, v[0] / L, 0.0]
        amt = float(mv.get("amount", 0.4))  # 推拉：走掉机位到主体距离的比例；横移 / 升降：米 = amount × 距离
        if kind in ("push", "pull"):
            d = L * amt * (1 if kind == "push" else -1)
            end = [p[0] + f[0] * d, p[1] + f[1] * d, p[2]]
        elif kind in ("truck_left", "truck_right"):
            d = L * amt * (1 if kind == "truck_left" else -1)
            end = [p[0] + side[0] * d, p[1] + side[1] * d, p[2]]
        elif kind in ("rise", "drop"):
            end = [p[0], p[1], max(0.3, p[2] + L * amt * (1 if kind == "rise" else -1))]
        else:
            ang = math.radians(35 * amt / 0.4) * (1 if kind == "orbit_left" else -1)
            dx, dy = p[0] - lk[0], p[1] - lk[1]
            end = [lk[0] + dx * math.cos(ang) - dy * math.sin(ang), lk[1] + dx * math.sin(ang) + dy * math.cos(ang), p[2]]
        return [round(x, 4) for x in end]

    def _camera(self, s, t0):
        t, size = s["type"], s.get("size")
        if s.get("cam") and s["cam"].get("mount"):  # 挂在车上、人身上的机位（车内、车头、肩扛跟拍）
            return self.mounted_cam(s, t0, s["cam"])
        if s.get("cam"):  # 逃生口：直接给机位
            c = s["cam"]
            lk = c["look"] if isinstance(c["look"], str) and (c["look"].startswith("prop_") or c["look"] in self.c.get("cast", {})) else self.pt(c["look"])  # "prop_花轿" / 人名：镜头盯住移动的道具或人，look_off 偏开一点（盯脚写 [0, 0, -1.2]）
            cm = K.cam(t0, c.get("lens", 35), self.pt(c["pos"]), lk, self.pt(c["end"]) if c.get("end") else None,
                       t0 + s["dur"] if c.get("end") else None)
            if c.get("look_off"):
                cm["look_off"] = c["look_off"]
            if c.get("end") and not isinstance(lk, str) and c.get("ease", True):  # 盯固定点的手写运镜缓入缓出；跟着移动的人或道具走的保持匀速，不然构图前后漂
                cm["ease"] = True
            return cm
        if t == "wide":
            e = self.loc["stage"]["views"][s["view"]] if s.get("view") else self.loc["stage"]["establish"]
            p, lk, lens = self.pt(e["pos"]), self.pt(e["look"]), e.get("lens", 35)
            if self.vertical and s.get("who") and not s.get("keep_view"):
                mv = s.get("move") if (s.get("move") or {}).get("kind") in MOVES else None
                p, lk, lens = self.fit_vertical(p, lk, lens, s["who"], t0, s["dur"], mv)
            return K.cam(t0, lens, p, lk)
        subj = s.get("subject")
        if t == "insert":
            o = self.pt(s["object"])
            if s.get("aim"):  # 对准人身上的点（"秀英.脚" / "秀英.手"），取这一镜中间时刻的位置：人在走、在被拖，静态标记点会对空（《好日子》第 6 条镜 8）
                who, part = (s["aim"].split(".") + ["脚"])[:2]
                q = self.pos(who, t0 + s["dur"] * s.get("aim_t", 0.5))  # aim_t：取这一镜里哪个时刻的位置（0–1，默认中间）
                fd = self.face_dir(who, t0 + s["dur"] * s.get("aim_t", 0.5))
                hh = self.height(who)
                if part == "脚":
                    o = [q[0], q[1], 0.05]
                else:  # 手：垂在身侧的右手（左手写"人名.左手"），不是身体中心（高难镜头测试第 2 条镜 3 拍成了胸口）
                    sgn = -1.0 if "左" in part else 1.0
                    o = [q[0] - fd[1] * 0.12 * hh * sgn * -1, q[1] + fd[0] * 0.12 * hh * sgn * -1, 0.45 * hh]
                off = s.get("cam_offset", [0.0, -0.45, 0.6] if part == "脚" else [-0.25, -0.6, 0.25])  # 偏移写在人自己的朝向里（-y 是人的正前方）：人转身，机位跟着转，不会钻进身体（高难镜头测试第 2 条镜 3）
                ca, sa = -fd[1], fd[0]
                off = [off[0] * ca - off[1] * sa, off[0] * sa + off[1] * ca, off[2]]
                return K.cam(t0, 50, K.add(o, off), o)
            off = s.get("cam_offset", [0.0, -0.45, 0.6])
            return K.cam(t0, 50, K.add(o, off), o)
        h = self.height(subj)
        if t == "ots":
            lens = SIZES[size or "中景"][0]
            fg, tg = self.pos(s["fg"], t0), self.pos(subj, t0)
            axis = self.axis_pts(t0)
            p = K.ots(fg, self.height(s["fg"]), tg, axis, back=s.get("back", 0.9), lat=s.get("lat", 0.3))
            look = K.head(tg, h)
            if not s.get("center"):  # 过肩：被拍的人落在前景肩膀的另一侧（38% / 62%）
                look = golden(p, look, lens, [fg[0] - p[0], fg[1] - p[1], 0.0], ratio=0.1, hw=self.hw)
            return K.cam(t0, lens, p, look)
        if t == "pov":  # 主观镜头挂在人头上跟着走，本人不入画；looks 让视线按时间转（回头、转回来）（4.0 alpha.12）
            eye = lambda tt: K.head(self.pos(subj, tt), h)  # noqa: E731
            lks = self.look_keys(eye, s.get("looks") or [{"t": 0, "at": s["look"]}], t0)
            p = eye(t0)
            cm = K.cam(t0, SIZES[size or "中景"][0], p, self._yaw_point(p, lks[0]))
            sp = math.dist(self.pos(subj, t0)[:2], self.pos(subj, t0 + s["dur"])[:2]) / s["dur"]
            cm.update(mount=subj, mount_off=[0, 0, 0], looks=lks, hide=[subj])
            if sp > 0.8:  # 走、跑时镜头上下颠
                cm["bob"] = 0.045 if sp > 2.2 else 0.018
            return cm
        lens, dist, hr = SIZES[size or ("近景" if t == "single" else "中景")]
        pair = next(((x["fg"], x["subject"], x) for x in self.c["shots"] if x["type"] == "ots"), None)
        other = None
        if pair and subj in pair[:2]:  # 正反打：单人镜头的机位和过肩在轴线同一侧，两个人一左一右（docs/21 第五节）
            other = pair[1] if subj == pair[0] else pair[0]
            fa, fb = self.pos(pair[0], t0), self.pos(pair[1], t0)
            po = K.ots(fa, self.height(pair[0]), fb, (fa, fb), back=pair[2].get("back", 0.9), lat=pair[2].get("lat", 0.3))
            ax_sign = 1.0 if (fb[0] - fa[0]) * (po[1] - fa[1]) - (fb[1] - fa[1]) * (po[0] - fa[0]) >= 0 else -1.0

        swing = 0.0
        if t in ("track", "single"):  # 正面有别人挡着：跟拍被推着后退的人（高难镜头测试第 2 条镜 4）、单人镜头前面站着拦路的人（场面调度测试第 1 条镜 7）→ 转开
            for tt in (t0, t0 + s["dur"] / 2, t0 + s["dur"]):
                pos0, d0 = self.pos(subj, tt), self.face_dir(subj, tt)
                for o2 in self.c.get("cast", {}):
                    if o2 == subj or self.c["cast"][o2].get("hidden") or self.chars.get(o2, {}).get("kind") in ("giant", "voice"):
                        continue
                    q2 = self.pos(o2, tt)
                    v2 = [q2[0] - pos0[0], q2[1] - pos0[1]]
                    L2 = math.hypot(*v2)
                    tall = q2[2] + 0.9 * self.height(o2) > pos0[2] + 0.9 * h - 0.3  # 头够得到主体的视线才算挡；坐着、蹲着的人不挡（《好日子》秀英站在坐着的阿禾身后梳头）
                    if tall and L2 < dist * 1.2 and (v2[0] * d0[0] + v2[1] * d0[1]) > 0.55 * L2:
                        swing = 1.0 if (d0[0] * v2[1] - d0[1] * v2[0]) < 0 else -1.0  # 转到另一个人不在的那一侧
            swing = s.get("swing", swing)
        sw_ang = math.radians(90 if t == "track" else 50) * swing  # 跟拍转到正侧面；单人镜头只转 50°，还是看得到脸

        def front(tt):
            pos, d = self.pos(subj, tt), self.face_dir(subj, tt)
            if swing:
                d = [d[0] * math.cos(sw_ang) - d[1] * math.sin(sw_ang), d[0] * math.sin(sw_ang) + d[1] * math.cos(sw_ang), 0.0]
            side = [-d[1], d[0], 0]
            if other:
                fa, fb = self.pos(pair[0], tt), self.pos(pair[1], tt)
                c = [pos[0] + side[0], pos[1] + side[1]]
                if ((fb[0] - fa[0]) * (c[1] - fa[1]) - (fb[1] - fa[1]) * (c[0] - fa[0]) >= 0) != (ax_sign > 0):
                    side = [-side[0], -side[1], 0]
            return [pos[0] + d[0] * dist + side[0] * dist * 0.2, pos[1] + d[1] * dist + side[1] * dist * 0.2, pos[2] + h * hr], K.head(pos, h)
        p0, look = front(t0)
        if t == "single" and size not in ("特写", "大特写") and not s.get("center"):  # 黄金分割：人头放在画面 38% / 62%，不放正中（docs/21 第五节）
            if other:
                op = self.pos(other, t0)
                look = golden(p0, look, lens, [op[0] - p0[0], op[1] - p0[1], 0.0], hw=self.hw)
            else:
                look = golden(p0, look, lens, self.face_dir(subj, t0), hw=self.hw)
        if t == "track":  # 镜头盯住人走（注视点跟着人动），不是盯着开头那一刻的位置
            p1, _ = front(t0 + s["dur"])
            return K.cam(t0, lens, p0, subj, p1, t0 + s["dur"])
        return K.cam(t0, lens, p0, look)

    def fit_vertical(self, p, look, lens, names, t0, dur, mv=None, margin=0.82):
        """竖屏全景自动机位（docs/22 第七节）：横屏的全景机位放进 9:16，两侧的人会出画。
        和几何检查取同样的时刻（25% / 50% / 75%），连运镜一起算：先把机位转向这些人的正中，
        装不下再换广一点的镜头（最广 18 mm），还装不下就沿视线往后退（最多 20 米）。"""
        people = [n for n in names if n in self.c.get("cast", {}) and self.chars.get(n, {}).get("kind") not in ("giant", "voice")]
        if not people:
            return p, look, lens
        fr = (0.25, 0.5, 0.75)
        heads = [[K.head(self.pos(n, t0 + x * dur), self.height(n)) for n in people] for x in fr]

        def angles(pp, lk):
            end = self.move_end(pp, lk, mv) if mv else pp
            out = []
            for x, hs in zip(fr, heads):
                c = [pp[i] + (end[i] - pp[i]) * x for i in range(3)]
                v = [lk[0] - c[0], lk[1] - c[1]]
                L = math.hypot(*v) or 1.0
                f, r = [v[0] / L, v[1] / L], [v[1] / L, -v[0] / L]
                out += [math.atan2((q[0] - c[0]) * r[0] + (q[1] - c[1]) * r[1], (q[0] - c[0]) * f[0] + (q[1] - c[1]) * f[1]) for q in hs]
            return min(out), max(out)
        lim = lambda ln: math.atan(margin * self.hw / ln)  # noqa: E731
        lo, hi = angles(p, look)
        if -lim(lens) <= lo and hi <= lim(lens):
            return p, look, lens
        def turn(lk, ang):  # 只转水平方向，高度不变
            v = [lk[0] - p[0], lk[1] - p[1]]
            d = math.hypot(*v) or 1.0
            f, r = [v[0] / d, v[1] / d], [v[1] / d, -v[0] / d]
            ff = [f[0] * math.cos(ang) + r[0] * math.sin(ang), f[1] * math.cos(ang) + r[1] * math.sin(ang)]
            return [p[0] + ff[0] * d, p[1] + ff[1] * d, lk[2]], ff
        for _ in range(3):  # 装得下就只转最少的角度，尽量保住原来的构图；装不下才转到正中
            lo, hi = angles(p, look)
            m = lim(lens)
            ang = (lo + hi) / 2 if hi - lo > 2 * m else (hi - m if hi > m else (lo + m if lo < -m else 0.0))
            if abs(ang) < 1e-4:
                break
            look, ff = turn(look, ang)
        look, ff = turn(look, 0.0)
        steps = sorted((x for x in K.LENS_DEG if isinstance(x, (int, float)) and 18 <= x < lens), reverse=True)  # 只用有视场角写法的焦段
        while steps and max(-angles(p, look)[0], angles(p, look)[1]) > lim(lens):
            lens = steps.pop(0)
        for _ in range(40):  # 每次后退 0.5 米
            lo, hi = angles(p, look)
            if max(-lo, hi) <= lim(lens):
                break
            p = [p[0] - ff[0] * 0.5, p[1] - ff[1] * 0.5, p[2]]
            look = [look[0] - ff[0] * 0.5, look[1] - ff[1] * 0.5, look[2]]
        return [round(x, 4) for x in p], [round(x, 4) for x in look], lens

    def axis_pts(self, t):
        pair = next(((s["fg"], s["subject"]) for s in self.c["shots"] if s["type"] == "ots"), None)
        return (self.pos(pair[0], t), self.pos(pair[1], t)) if pair else None

    def optics(self, s, lens, cm):
        """有全片影调（设定 film_look）时，镜头写成电影摄影的说法：焦段 + 镜头类型 + 视场角、机位高度与俯仰、景深（4.0 alpha.14，《好日子》质感复盘）。
        没写 film_look 的老项目保持"84°"的写法，产物不变。"""
        fl = self.b.get("film_look")
        deg = K.LENS_DEG.get(lens, f"{lens}mm")
        if not fl:
            return deg
        c = cam_preset(self.b, self.loc["world"], s)
        base_c = cam_preset(self.b, self.loc["world"])
        if c:  # 镜头组 + 这一镜的焦段；单镜换了摄影机预设就写出来
            out = (f"这一镜换成 {c['camera']}，" if c is not base_c else "") + f"{c['lens']} {lens}mm（视场约 {deg}）"
            if c is not base_c:
                out += f"，{c['effects_zh']}"
        else:
            out = f"{lens}mm {fl.get('lens_zh', '')}镜头（视场约 {deg}）".replace("  ", " ")
        if cm and cm.get("keys"):
            p = cm["keys"][0]["pos"]
            la = cm.get("look_at")
            lk = la if isinstance(la, list) else None
            if lk:
                dz, hz = p[2] - lk[2], math.hypot(lk[0] - p[0], lk[1] - p[1]) or 1e-6
                pitch = math.degrees(math.atan2(dz, hz))
                h = p[2] - self.origin[2]
                ht = "贴地" if h < 0.5 else ("腰高" if h < 1.1 else ("人眼高" if h < 1.9 else ("高处" if h < 6 else "大俯瞰")))
                ang = "仰拍" if pitch < -6 else ("俯拍" if pitch > 6 else "平视")
                out += f"，机位{ht}{ang}"
        sz = s.get("size") or ("特写" if s.get("type") == "insert" else "")
        out += {"大特写": "，极浅景深", "特写": "，浅景深，背景完全虚化", "近景": "，浅景深，背景虚化", "中景": "，背景轻微虚化"}.get(sz, "，景深较深")
        return out

    def body(self, s, lens, t0=0.0, cm=None):
        b, t = [], s["type"]
        world = self.loc["world"]
        if t == "ots":
            b.append(f"画面一侧前景{s['fg']}虚化的肩膀")
        if t == "insert":
            b.append(f"{s['hand']}和{s['object']}" if s.get("hand") else s["object"])
        for n in ([s["subject"]] if s.get("subject") and t not in ("insert", "pov") else []) + [w for w in s.get("who", []) if w not in (s.get("subject"), s.get("fg"))]:
            b.append(n)  # 主观镜头：主观者本人不入画，不写进"画面里"（模型会把她画出来）
        b += [n for n in s.get("see", []) if n not in b]  # 画面里的车、道具（只有车的追逐镜头）
        if t == "pov" and not b:  # 主观镜头里没有人：写视线落在哪里（"走廊尽头"），不留空
            ats = [x["at"] for x in (s.get("looks") or [{"at": s.get("look")}]) if isinstance(x.get("at"), str)]
            b += list(dict.fromkeys(a for a in ats if a not in self.c.get("cast", {})))[-1:]
        if s.get("crowd"):
            ids = "、".join(map(str, s["crowd"].get("ids", [])))
            b.append(f"{world}群演（{ids} 号）{s['crowd'].get('doing', '')}")
        head = "画面里：" + "、".join(b) + "。"
        deg = self.optics(s, lens, cm)
        mv = s.get("move")
        base = MOVE[t] if t != "pov" else f"{s['subject']}的主观视角，{s['subject']}自己不入画"
        if t == "insert":  # 插入镜头按 cam_offset 写俯拍 / 仰拍；景别不是特写时不写"特写"（《好日子》第 7 条仰拍岩壁裂缝）
            off = s.get("cam_offset", [0.0, -0.45, 0.6])
            base = ("仰拍" if off[2] < 0 else "俯拍") + ("特写" if s.get("size") in (None, "特写", "大特写") else "")
        cmv = self.cam_move(s)
        if cmv:  # 手写机位、挂载机位在动：照白模写，不能写"固定"（白模和文字打架）
            base = cmv["zh"] if t != "pov" else f"{base}，{cmv['zh']}"
        mtxt = base if not mv else (move_text(mv) if t in ("wide", "single") else f"{base}，{move_text(mv)}")
        parts = [f"{deg}，{s.get('size', '') + '，' if s.get('size') else ''}{mtxt}"]
        if s.get("comp"):  # 构图手法（前景框、纵深、留白、剪影、对称……）
            parts.append(f"构图：{s['comp']}")
        if s.get("light"):  # 这一镜的布光：主光方向、明暗比、轮廓光、雾气、实景光源
            parts.append(f"光：{s['light']}")
        acts = s.get("action", [])
        for a in (acts if isinstance(acts, list) else [acts]):
            parts.append(f"第 {a['at']} 秒，{a['text']}" if isinstance(a, dict) and "at" in a else (a["text"] if isinstance(a, dict) else a))
        timed = []
        for bt in s.get("beats", []):  # 节拍的文字和白模同一个来源：第几秒、谁、做什么
            if bt.get("text"):
                timed.append((bt["t"], f"第 {round(t0 + bt['t'], 2):g} 秒，{bt['text']}"))  # 整条的秒数，和时间轴、白模一致（节拍里的 t 是镜内秒数）
        prev_at = None
        for L in (s.get("looks") or (s.get("cam") or {}).get("looks") or []):  # 视线转动写进时间轴（第一人称回头、转回来），和白模同一来源
            nm = self._look_name(L["at"])
            if prev_at is not None and nm and nm != prev_at and L.get("t", 0) > 0:
                timed.append((L["t"], f"第 {round(t0 + L['t'], 2):g} 秒，视线转向{nm}"))
            prev_at = nm if nm else prev_at
        parts += [x for _t, x in sorted(timed, key=lambda q: q[0])]
        if s.get("hit"):
            h = s["hit"]
            parts.append(f"动作因果：{h['support']}，{h['force']}，{h['contact']}，{h['reaction']}，{h['result']}")
        if (s.get("acting") or {}).get("leak"):
            parts.append(f"表演：{s['acting']['leak']}")
        if s.get("vfx"):
            v = s["vfx"]
            parts.append(f"特效：从{v['source']}发出，{v['direction']}，{v['scale']}，{v['speed']}；{v['medium']}；{v['light']}；{v['decay']}")
        if s.get("line"):
            ln = s["line"]
            how = f"（{ln['delivery']}）" if ln.get("delivery") else ""
            say = {"on": f"{ln['who']}{how}说：", "back": f"背对镜头的{ln['who']}{how}说：", "off": f"画外，{ln['who']}的声音{how}："}[ln.get("mode", "on")]
            parts.append(f"{say}{{{ln['text']}}}")
        snd = " ".join(f"<{x['text']}>" for x in s.get("sound", []))
        out = head + "；".join(parts) + (f"；{snd}" if snd else "")
        return out if out.endswith("}") else out + "。"

    def proxies(self):
        """插入镜头的白模代理物（4.0 alpha.8，《好日子》实测）：白模里没有道具和手，特写就只拍到地面、桌面、墙，
        模型拿到一段灰屏当 @视频1。每个插入镜头在 object 点放一块灰白方块（道具），写了 hand 再放一块肉色小块（手），只在这一镜出现。
        尺寸可用 proxy: [宽, 深, 高]、颜色 proxy_color 覆盖。"""
        out, t0 = [], 0.0
        for k, s in enumerate(self.c["shots"], 1):
            t1 = t0 + s["dur"]
            if s["type"] == "insert" and s.get("object") and not s.get("no_proxy") and not s.get("beats"):  # 有节拍的特写用真的手和道具
                p = self.pt(s["object"])
                w, d, h = s.get("proxy", [0.32, 0.24, 0.12])
                keys = K.appear(max(t0, 0.02), t1)
                out.append(K.b(f"代理_{k}_{s['object']}", [p[0] - w / 2, p[1] - d / 2, p[2]], [p[0] + w / 2, p[1] + d / 2, p[2] + h],
                               BOXCOLOR.get(s.get("proxy_color", "white"), BOXCOLOR["white"]), keys))
                if s.get("hand"):
                    out.append(K.b(f"代理_{k}_手", [p[0] + 0.06, p[1] - 0.2, p[2] + h], [p[0] + 0.15, p[1] - 0.02, p[2] + h + 0.05], BOXCOLOR["skin"], keys))
            t0 = t1
        return out

    def layout(self, s, t0, cm, ppl):
        """不喂白模时（万相 3.0 默认，4.0 alpha.11）：从机位和站位算出每个人在画面哪一侧、远近、往哪走，写进提示词。"""
        def at(person, t):
            ks = person["keys"]
            if t <= ks[0]["t"]:
                return ks[0]["pos"]
            for a, b in zip(ks, ks[1:]):
                if a["t"] <= t <= b["t"]:
                    f = 0 if b["t"] == a["t"] else (t - a["t"]) / (b["t"] - a["t"])
                    return [a["pos"][i] + (b["pos"][i] - a["pos"][i]) * f for i in range(3)]
            return ks[-1]["pos"]
        cp = cm["keys"][0]["pos"]
        la = cm["look_at"]
        lk = (self.pos(la, t0) if la in self.c["cast"] else self.pt(la)) if isinstance(la, str) else la
        fx, fy = lk[0] - cp[0], lk[1] - cp[1]
        n = math.hypot(fx, fy) or 1.0
        fx, fy = fx / n, fy / n
        rx, ry = fy, -fx  # 画面右方
        half = math.tan(math.radians(K.hfov(cm["lens"]) / 2)) if hasattr(K, "hfov") else 36 / 2 / cm["lens"]

        def screen(q):
            dx, dy = q[0] - cp[0], q[1] - cp[1]
            depth = dx * fx + dy * fy
            return (dx * rx + dy * ry) / max(depth, 0.1) / half, depth

        def side(x):
            if abs(x) <= 0.12:
                return "画面中间"
            return ("画面左侧" if x < 0 else "画面右侧") if abs(x) > 0.45 else ("画面中间偏左" if x < 0 else "画面中间偏右")  # 过肩镜里前景在边上、主体在中间偏一侧，分开写才不会两个人都是"右侧"

        def move(a, b, da, db):
            out = []
            if abs(b - a) > 0.25:
                out.append("朝画面右边" if b > a else "朝画面左边")
            if abs(db - da) > 0.8:
                out.append("走远" if db > da else "朝镜头走近")
            return "、".join(out)
        t1, tm = t0 + s["dur"], t0 + s["dur"] / 2
        names = [n2 for n2 in ([s.get("fg"), s.get("subject")] + list(s.get("who") or [])) if n2]
        rows = []
        byname = {p_["name"]: p_ for p_ in ppl}
        for n2 in dict.fromkeys(names):
            if n2 not in byname:
                continue
            (xa, da), (xm, dm), (xb, db) = (screen(at(byname[n2], t)) for t in (t0, tm, t1))
            rows.append({"who": n2, "x": round(xm, 2), "side": side(xm), "near": "近处" if dm < 4 else ("远处" if dm > 12 else "中景处"), "move": move(xa, xb, da, db)})
        crowd = [p_ for p_ in ppl if p_["name"].startswith("群演")]
        cr = None
        if crowd and s.get("crowd"):
            ca = [screen(at(p_, t0)) for p_ in crowd]
            cb = [screen(at(p_, t1)) for p_ in crowd]
            xa = sum(x for x, _ in ca) / len(ca)
            xb = sum(x for x, _ in cb) / len(cb)
            cr = {"side": side((xa + xb) / 2), "move": move(xa, xb, 0, 0)}  # 人群只写左右
        hz = cp[2] - (lk[2] if len(lk) > 2 else 1.5)
        pitch = "高机位俯拍" if hz > 2.5 else ("低机位仰拍" if hz < -0.6 else "平视")
        return {"people": rows, "crowd": cr, "pitch": pitch}

    def build(self, assets_idx):
        shots, t0 = [], 0.0
        ppl_all = self.people()
        for k, s in enumerate(self.c["shots"], 1):
            cm = self.camera(s, t0)
            sh = dict(t0=round(t0, 3), t1=round(t0 + s["dur"], 3), name=s.get("name", f"镜{k}"), lens=cm["lens"], cam=cm, body=self.body(s, cm["lens"], t0, cm))
            glass = [f"{self.loc['name']}_{bx['name']}" for bx in self.loc["stage"].get("boxes", []) if bx.get("color") == "glass"]
            occ = list(s.get("allow_occlude", [])) + glass  # 玻璃是透明的，挡在前面不算遮挡
            if occ:
                sh["allow_occlude"] = occ
            if s.get("allow_small"):
                sh["allow_small"] = s["allow_small"]
            if s.get("allow_exit"):
                sh["allow_exit"] = s["allow_exit"]
            if s.get("see"):  # 画面里要拍到的车和道具：几何检查按道具查入画、遮挡、大小
                sh["see"] = ["prop_" + n for n in s["see"]]
            if s.get("enter"):  # 长镜头里中途才入画的人：{"老板": 镜内秒}
                sh["enter"] = s["enter"]
            acts = s.get("action", [])
            acts = acts if isinstance(acts, list) else [acts]
            sh["struct"] = {k: v for k, v in dict(
                type=s["type"], size=s.get("size"), lens=cm["lens"], subject=s.get("subject"), fg=s.get("fg"), who=s.get("who"),
                object=s.get("object"), object_en=s.get("object_en"), hand=s.get("hand"), hand_en=s.get("hand_en"), view=s.get("view"),
                action=[{"at": a.get("at"), "zh": a.get("text"), "en": a.get("en")} if isinstance(a, dict) else {"zh": a} for a in acts],
                line=s.get("line"), sound=[{"zh": x.get("text"), "en": x.get("en"), "source": x.get("source")} for x in s.get("sound", []) if isinstance(x, dict)],
                crowd=s.get("crowd"), beats=s.get("beats"), hit_en=(s.get("hit") or {}).get("en"), vfx_en=(s.get("vfx") or {}).get("en"), acting_en=(s.get("acting") or {}).get("leak_en"), acting=(s.get("acting") or {}).get("leak"),
                move=({"kind": s["move"]["kind"], "en": move_text(s["move"], "en"), "zh": move_text(s["move"])} if s.get("move") and s["move"].get("kind") in MOVES
                      else (dict(self.cam_move(s), kind="cam") if self.cam_move(s) else None))).items() if v}  # 结构化镜头：海螺 / 万相的专用编译器用
            poses = {n2: self.c["cast"][n2]["pose"] for n2 in dict.fromkeys([s.get("fg"), s.get("subject")] + list(s.get("who") or []))
                     if n2 and self.c["cast"].get(n2, {}).get("pose")}
            if poses:  # 这一镜画里每个人的姿势和状态；不喂白模时每镜都要重说一遍（《好日子》第 2 条：阿禾一镜站着盘发、一镜坐着披发）
                sh["struct"]["poses"] = poses
            try:
                sh["struct"]["layout"] = self.layout(s, t0, cm, ppl_all)
            except Exception as e:  # noqa: BLE001  算不出就不写
                sh["struct"]["layout_err"] = str(e)[:80]
            shots.append(sh)
            t0 += s["dur"]
        c = self.c
        hidden = {n: v["hidden"] for n, v in c["cast"].items() if v.get("hidden")}
        cast = [n for n in c["cast"] if n not in hidden]
        world = self.loc["world"]
        tags = [f"@{n}" for n in cast] + [f"@prop_{p}" for p in c.get("props", [])] + [f"@loc_{c['location']}"]
        if f"@loc_{c['location']}_grid" in assets_idx:
            tags.append(f"@loc_{c['location']}_grid")
        if any(s.get("crowd") for s in c["shots"]):
            tags.append(f"@crowd_{world}")
        speakers = []
        for s in c["shots"]:
            if s.get("line") and s["line"]["who"] not in speakers:
                speakers.append(s["line"]["who"])
        brief = "、".join(f"{n}（{self.chars[n].get('mark_zh', self.chars[n]['lock'].split('：', 1)[-1][:12])}）" for n in cast)
        crowd_note = f"另有{world}群演，按编号取自群像。" if "@crowd_" + world in tags else ""
        ots = next((s for s in c["shots"] if s["type"] == "ots"), None)
        axis_txt = f"{ots['fg']}和{ots['subject']}对话的轴线固定，整场不换边；" if ots else ""
        ppl = self.people()
        proxy_map = []  # 白模里每个代理是谁（4.0 alpha.10）：编译器按它逐个写"紫色关节小人 → 图5「某某」"，不让模型猜
        for p in ppl:
            if p["name"].startswith("群演") or p["name"] not in self.chars:
                continue
            ci = PALETTE.index(p["color"]) if p["color"] in PALETTE else 0
            proxy_map.append({"name": p["name"], "tag": f"@{p['name']}", "color": PALETTE_ZH[ci],
                              "shape": "giant" if p.get("shape") == "giant" else ("cyl" if p.get("rig") is False else "rig")})
        return dict(n=c["n"], dur=round(t0, 3), title=c["title"], proxy_map=proxy_map, summary=c.get("summary", c["title"]), model=c.get("model", "Seedance 2.5 + 白模"),
                    tags=tags, count=f"画面中始终只有 {len(cast)} 个有名字的角色：{brief}。{crowd_note}"
                    + "".join(f"{n}{h}，全程看不见，画面里不出现。" for n, h in hidden.items()) + "每一镜画面里有谁，以时间轴里\"画面里\"那一句为准。",
                    context=c.get("context", c.get("summary", "")) + (
                        f"这场对话里，{c['drama']['a']}；{c['drama']['b']}；第 {c['drama']['shift']} 镜那句话之后，局面反过来。" if c.get("drama") else ""), extra_chars=[], voices=speakers,
                    sfx=" ".join(f"<{x['text']}>" for s in c["shots"] for x in s.get("sound", [])) or "无",
                    constraint=axis_txt + c.get("constraint", "") + "".join(
                        f"道具操作（{p['name']}）：{p['mechanism']}。" for p in self.b.get("props", []) if p["name"] in c.get("props", []) and p.get("mechanism")), axis={"a": ots["fg"], "b": ots["subject"]} if ots else None,
                    **({"blocking": c["blocking"]} if c.get("blocking") else {}),
                    **({"poses": {n: v["pose"] for n, v in c["cast"].items() if v.get("pose") and n not in hidden}} if any(v.get("pose") for v in c["cast"].values()) else {}),
                    people=ppl, shots=shots, extra_boxes=self.proxies(), props_previs=self.props_previs(), effects_previs=self.effects_previs(), light_previs=self.light_previs(), fail=c.get("fail", "人物脸和服装与参考图不一致；画面出现文字。"),
                    risk=c.get("risk", "—"), splice=c.get("splice", "—"), summary_en=c.get("summary_en"),
                    style_en=self.style_en)


# ---------------- 声音资产（docs/18 sound 阶段、crew/09） ----------------

VOICE_DEFAULT = "嗯，我在。这件事，我们慢慢说。"


def _seed(name):
    return sum(ord(ch) * (i + 1) for i, ch in enumerate(name)) % 100000


def voice_mode(proj):
    """音色参考：默认 off（模型按音色锁的文字自己出声，用户实测比本地 TTS 自然）；
    "user" = 用户提供真人录音（声音/音色_<角色>.wav）；"tts" = 用户明确要本地 TTS。production.json 的 voice_refs。"""
    root = os.path.dirname(proj) if series_kind(proj) == "episode" else proj
    p = os.path.join(root, "production.json")
    v = json.load(open(p, encoding="utf-8")).get("voice_refs", "off") if os.path.exists(p) else "off"
    return v if v in ("user", "tts") else "off"


def sound_items(bible, board, voices="off"):
    """每个场景一段环境底、可选配乐（后期铺底）；voices 不是 off 时，每个开口的角色再加一段音色参考（喂三家视频模型）。"""
    cm = char_map(bible)
    lines, used = {}, {}
    for c in board.get("clips", []):
        for sh in c.get("shots", []):
            ln = sh.get("line")
            if ln:
                b = cm.get(ln["who"], {}).get("_base", ln["who"])
                lines.setdefault(b, []).append(ln["text"])
                used.setdefault(b, []).append(str(c.get("n")))
    out = []
    for ch in bible.get("characters", []) if voices != "off" else []:
        n = ch["name"]
        if n not in lines or not ch.get("voice"):
            continue
        txt = ch.get("voice_sample")
        if not txt:
            acc = ""
            for t in lines[n]:  # 5–9 秒：海螺、万相总共只收 15 秒，三个人都要放得下
                if len(SEP.sub("", acc)) >= 12 or len(SEP.sub("", acc + t)) > 24:
                    break
                acc += ("" if not acc else "\n") + t
            txt = acc if len(SEP.sub("", acc)) >= 10 else VOICE_DEFAULT
        who = ch.get("lock", "").split("：", 1)[-1].split("，")[0] if ch.get("kind") != "voice" else ""
        out.append({"kind": "voice", "name": n, "file": f"声音/音色_{n}.wav", "dur": [5, 9] if voices == "tts" else [3, 30], "clips": sorted(set(used[n]), key=used[n].index),
                    "tool": "local-tts design_voice" if voices == "tts" else "用户提供的真人录音（5–30 秒，干净、无配乐，自然说话）",
                    "args": {"voice_description": (who + "；" if who else "") + ch["voice"], "text": txt, "language": "chinese",
                             "seed": _seed(n), "temperature": 0.7, "title": f"音色_{n}"}})
    for loc in bible.get("locations", []):
        sfx = []
        for c in board.get("clips", []):
            if c.get("location") != loc["name"]:
                continue
            for sh in c.get("shots", []):
                for sd in sh.get("sound", []):
                    if isinstance(sd, dict) and sd.get("source") == "画外" and sd.get("en") and sd["en"] not in sfx \
                            and not re.search(r"voice|says|announce|speech|drain|ting|silence|beep|crash|slam|scream", sd["en"], re.I):  # 铺底只要持续的环境声，不要人声和单次事件
                        sfx.append(sd["en"])
        if not any(c.get("location") == loc["name"] for c in board.get("clips", [])):
            continue
        out.append({"kind": "ambience", "name": loc["name"], "file": f"声音/环境_{loc['name']}.wav", "dur": [8, 40],
                    "tool": "audio-studio generate_audio",
                    "args": {"audio_type": "sfx", "duration_seconds": 20, "title": f"环境_{loc['name']}",
                             "prompt": loc.get("ambience_en") or (f"Continuous background ambience, no music, no voices: {loc['en']} " + ("; ".join(sfx[:4]) + "." if sfx else "")),
                             "sound_space": "wide stereo room tone, distant, steady, loopable"}})
    if bible.get("score_en"):
        out.append({"kind": "score", "name": "配乐", "file": "声音/配乐.wav", "dur": [30, 400], "tool": "audio-studio generate_audio",
                    "args": {"audio_type": "music", "duration_seconds": 120, "title": "配乐", "prompt": bible["score_en"]}})
    return out


def build_sound(proj):
    bible, board = load(proj)
    items = sound_items(bible, board, voice_mode(proj))
    L = [f"# 《{bible['title']}》声音资产", "",
         "> 流水线生成，不要手改。环境底和配乐用本机 audio-studio 的 generate_audio（免费），**只在后期铺底**（`tools/post.py --ambience`），生成视频时不要配乐。",
         "> **对白音色**默认交给视频模型按音色锁的文字自己出（用户实测：本地 TTS 像配音、会带方言腔，不如模型自带的自然）。只有 production.json 写 `voice_refs: \"user\"`（真人录音）或 `\"tts\"` 时才有音色参考，并喂给三家模型。",
         "> 生成后存成 48 kHz WAV 放到 `交付/` 下对应路径，再跑 run；流水线只做技术检查（时长、响度、削波），**真正好不好听要人听**。", "",
         "| 序 | 类型 | 名称 | 文件 | 用在 | 工具 |", "|---|---|---|---|---|---|"]
    kz = {"voice": "音色参考", "ambience": "环境底", "score": "配乐"}
    for i, it in enumerate(items, 1):
        L.append(f"| {i} | {kz[it['kind']]} | {it['name']} | {it['file']} | {('第 ' + '、'.join(it['clips']) + ' 条') if it.get('clips') else '后期'} | {it['tool']} |")
    L.append("")
    for i, it in enumerate(items, 1):
        L += [f"### {i}｜{kz[it['kind']]}｜{it['name']}", "", f"- 文件：`交付/{it['file']}`（时长 {it['dur'][0]}–{it['dur'][1]} 秒）", f"- 工具：{it['tool']}，参数：", "",
              "```json", json.dumps(it["args"], ensure_ascii=False, indent=1), "```", ""]
    os.makedirs(os.path.join(proj, "交付"), exist_ok=True)
    open(os.path.join(proj, "交付", "02-声音资产.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump(items, open(os.path.join(proj, "交付", "声音清单.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return len(items), []


def build_assets(proj):
    bible, board = load(proj)
    E, W = validate(bible, board, models_of(proj))
    if E:
        raise Bad("\n".join(E))
    en, _, note = style_map(bible)
    its = items(bible, board)
    K.write_asset_doc(proj, bible["title"], its, en, note, "出图顺序：人物三视图 → 状态变体 → 道具 → 场景 → 宫格 → 群像。")
    return len(its), W


def build_stress(proj):
    """角色压力测试（crew/07）：每个主角 3 张——本片光下 3/4 中近景、暗处全身远景、和另一个主角同框。
    写 交付/01b-角色压力测试.md；前面照抄主角三视图条目（已出好的图会被跳过，当作参考图）。"""
    bible, board = load(proj)
    en, _, _ = style_map(bible)
    film = bible["film"]
    sheets = [it for it in items(bible, board) if it["type"] == "人物三视图"]
    locs = bible["locations"]
    out = [dict(it) for it in sheets]
    idx = {it["tag"]: i + 1 for i, it in enumerate(out)}
    mains = [c for c in bible["characters"] if not c.get("kind")]
    for k, c in enumerate(mains):
        me = idx[f"@{c['name']}"]
        la, lb = locs[k % len(locs)], locs[(k + 1) % len(locs)]
        partner = mains[(k + 1) % len(mains)] if len(mains) > 1 else None
        same = "Image 1 is the approved character sheet: render this exact person (face from the large portrait on the right), " \
               "same face, hair, costume and body; clearly an adult; no text anywhere."
        tests = [("3/4 中近景 · " + la["name"] + "的光", f"A cinematic photoreal still of a {film}: a three-quarter medium close-up of {c['brief_en']} "
                  f"inside {la['en']}", f"{same} Lit by the scene's own light: {la.get('light', '')}.", la["world"], str(me)),
                 ("暗处全身远景 · " + lb["name"], f"A cinematic photoreal still of a {film}: a wide full-body shot of {c['brief_en']} standing in a dim corner of {lb['en']}",
                  f"{same} Low-key light; the person is small in frame but still recognisable by silhouette: {c.get('silhouette', '')}.", lb["world"], str(me))]
        if partner:
            tests.append(("双人同框 · " + partner["name"], f"A cinematic photoreal still of a {film}: a medium two-shot of {c['brief_en']} and {partner['brief_en']} "
                          f"facing each other inside {la['en']}", f"Images 1 and 2 are the approved character sheets of the two people; keep each face, "
                          "hair and costume exactly, never swap or blend them; both clearly adults; no text anywhere.", la["world"],
                          f"{me}、{idx['@' + partner['name']]}"))
        for j, (name, en_txt, must, world, deps) in enumerate(tests, 1):
            out.append(dict(tag=f"@压测_{c['name']}_{j}", world=world, file=f"压测_{c['name']}_{j}.png", type="压力测试", rec=REC, deps=deps,
                            use=name, lock=c["lock"], en=en_txt, must=must,
                            check=["脸和三视图大头像是同一个人", "发型、服装、剪影识别点都对", "皮肤自然、不像 AI 渲染", "没有文字"],
                            duty="只用于定稿前检查，不进视频"))
    path = os.path.join(proj, "交付")
    K.write_asset_doc(proj, bible["title"] + " · 角色压力测试", out, en, "与资产提示词相同的风格配方", "前几行是已定稿的三视图（会被跳过，只作参考图）。")
    os.replace(os.path.join(path, "01-资产提示词.md"), os.path.join(path, "01b-角色压力测试.md"))
    build_assets(proj)  # 恢复正式资产文档
    return len(out) - len(sheets), []


def build_specs(proj):
    bible, board = load(proj)
    E, W = validate(bible, board, models_of(proj))
    if E:
        raise Bad("\n".join(E))
    en, zh, _ = style_map(bible)
    its = items(bible, board)
    idx = {it["tag"] for it in its}
    loc_index = {l["name"]: i for i, l in enumerate(bible["locations"])}
    sp = os.path.join(proj if series_kind(proj) != "episode" else os.path.dirname(proj), "production.json")
    on = json.load(open(sp, encoding="utf-8")).get("sound_assets", True) if os.path.exists(sp) else True
    voiced = {it["name"] for it in sound_items(bible, board, voice_mode(proj)) if it["kind"] == "voice"} if on else set()
    clips, boxes = [], []
    seen, prev = set(), None
    for c in board["clips"]:
        cl = Clip(bible, c, loc_index[c["location"]])
        world = cl.loc["world"]
        vid = lambda x: x.replace(" still;", " footage;").replace(" still ", " footage ").replace("still shot on", "footage shot on")  # noqa: E731  配方原是出图用语
        rz = {r["id"]: r for r in json.load(open(os.path.join(ROOT, "config", "style_recipes.json"), encoding="utf-8"))["配方"]}  # 每条只写本条所在世界的质感（docs/16）
        wr = bible["worlds"][world]["recipe"]
        pr = bible["worlds"][world].get("people", bible["people_recipe"])  # 世界可以覆盖人物配方；和世界相同就只写一次
        ve = lambda r: r.get("video_en", r["en"])  # noqa: E731  视频只用配方的"质感版"：出图样片里的构图和画面内容（极小的人、巨大暗影……）不进视频（4.0 alpha.10）
        vz = lambda r: r.get("video_zh", r["zh"])  # noqa: E731
        cl.style_en = f"Setting look: {vid(ve(rz[wr]))}." + (f" People look: {vid(ve(rz[pr]))}." if ve(rz[pr]) != ve(rz[wr]) else "")
        style_zh = f"质感：{world}（{wr}）：{vz(rz[wr])}" + (f"；人物（{pr}）：{vz(rz[pr])}。" if pr != wr else "。")
        if bible.get("film_look"):  # 全片影调：每一条逐字一样，压在世界配方后面（4.0 alpha.14）
            if bible["film_look"].get("en"):
                cl.style_en += f" Film look: {bible['film_look']['en']}."
                style_zh += f"影调：{bible['film_look']['zh']}。"
            cpr = cam_preset(bible, world)
            if cpr:  # 成像逻辑：这个世界用的摄影机预设
                cl.style_en += f" Camera: {cam_line(cpr)}."
                style_zh += cam_line(cpr, "zh") + "。"
        clips.append(cl.build(idx))
        clips[-1]["style_zh"] = style_zh
        cm = char_map(bible)
        spk = []
        for sh in c["shots"]:
            if sh.get("line"):
                b = cm.get(sh["line"]["who"], {}).get("_base", sh["line"]["who"])
                if b not in spk:
                    spk.append(b)
        clips[-1]["audio"] = [{"tag": f"@voice_{b}", "file": f"声音/音色_{b}.wav", "name": b, "kind": "voice"} for b in spk if b in voiced]
        if prev and prev["location"] == c["location"] and not c.get("time_jump"):  # 连续的戏：上一条出片后的尾帧接进这一条（docs/07）
            clips[-1]["splice_in"] = {"file": f"衔接/第{prev['n']}条_尾帧.png", "from": prev["n"]}
        prev = c
        if c["location"] not in seen:
            boxes += cl.boxes()
            seen.add(c["location"])
    voice = {n: f"{n}：{c['voice']}" for n, c in char_map(bible).items() if c.get("voice")}
    voice_en = {c["name"]: c["voice_en"] for c in bible["characters"] if c.get("kind") == "voice" and c.get("voice_en")}
    texts = dict(THREE=bible["three"], STYLE=zh, MUSIC=bible["music"], MUSIC_EN=bible.get("music_en"), VOICE=voice, LIP=bible.get("lip", LIP),
                 ANCHOR=ANCHOR + bible.get("anchor_extra", ""), RATIO=bible.get("ratio", "16:9"))
    if voice_en:
        texts["VOICE_EN"] = voice_en
    os.makedirs(os.path.join(proj, "预演规格"), exist_ok=True)
    colors = "，".join((f"{PALETTE_ZH[i % 6]}色树干巨物（根须、空心环头）={c['name']}" if c.get("kind") == "giant" else f"{PALETTE_ZH[i % 6]}色圆柱={c['name']}")
                       for i, c in enumerate(x for x in bible["characters"] if x.get("kind") != "voice")) + "，灰白方块=场景和道具"
    K.export(proj, bible["title"], bible["short"], its, boxes, clips, texts, colors)
    return len(clips), W


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("check", "assets", "specs", "stress", "sound"):
        sys.exit(__doc__)
    proj = os.path.abspath(sys.argv[2])
    try:
        if sys.argv[1] == "check":
            E, W = validate(*load(proj), models_of(proj))
            for w in W:
                print("WARN ", w)
            for e in E:
                print("ERROR", e)
            print("通过" if not E else f"{len(E)} 处错误")
            sys.exit(1 if E else 0)
        n, W = {"assets": build_assets, "specs": build_specs, "stress": build_stress, "sound": build_sound}[sys.argv[1]](proj)
        for w in W:
            print("WARN ", w)
        print(f"完成：{n}")
    except Bad as e:
        print("ERROR", str(e).replace("\n", "\nERROR "))
        sys.exit(1)


if __name__ == "__main__":
    main()
