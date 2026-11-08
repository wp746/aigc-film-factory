#!/usr/bin/env python3
"""自动化流水线：剧本确认后，资产 → 出图 → 白模 → 闸门 → 投喂包，一条命令按固定顺序跑，任何智能体跑出来都一样。

  python3 tools/pipeline.py status <项目目录>            看进度，最后一行 NEXT 就是下一步该做什么（智能体只照 NEXT 做）
  python3 tools/pipeline.py run <项目目录> [--until 阶段]   从头按顺序跑；已完成且输入没变的阶段自动跳过；遇到需要人或智能体处理的就停
  python3 tools/pipeline.py confirm <项目目录> story|script  记录用户已确认故事 / 剧本（只有用户在对话里明确确认后才执行）
  python3 tools/pipeline.py approve <项目目录> images <张数>  记录用户批准的出图预算（只有用户在对话里明确同意花费后才执行）
  python3 tools/pipeline.py review <项目目录> <条目> pass|fix [备注]   记录目检结论；条目见 status 里的"待目检"
  python3 tools/pipeline.py redo <项目目录> <资产序号>        把一张不合格的图挪进废片，下次 run 重出（占用出图预算）
  python3 tools/pipeline.py init <项目目录> <片名> <短名>     新项目（高级写法）：从 templates/project/ 拷入两个 Python 数据脚本
  python3 tools/pipeline.py selftest examples/样例-便利店     自测：不花钱（占位图），从样例数据跑到投喂包，和标准产物逐字比对
  python3 tools/pipeline.py qc <项目目录> <第N条> <视频文件>   审片：切点、时长、分段音量、台词听写，写 logs/审片_第N条.md
  python3 tools/pipeline.py splice <项目目录> <第N条> <视频文件>  截这条视频的最后一帧到 交付/衔接/，下次 run 时接进下一条的投喂包（docs/07）
  python3 tools/pipeline.py coldread <项目目录> 故事|投喂前|成片   冷读任务书：交给一个不带上下文的子代理，写一页问题清单（templates/冷读.md）
  python3 tools/pipeline.py model <项目目录> wan[,hailuo,...]     切换出片模型：只编这几家的投喂包，并打印这家的写法要点和验证状态（docs/13 八·六）

阶段（顺序固定）：confirm → assets → images → asset_review → stress（可选）→ specs → previs → check_spec → geometry → framesheets
→ previs_review → compile → report。规则和每一步的含义见 docs/18-自动化流水线.md。
状态写在 <项目>/production.json 的 "pipeline" 和 "reviews" 里；产物与日志写在 <项目>/logs/。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
PY = sys.executable
STAGES = ["confirm", "assets", "images", "asset_review", "sound", "stress", "specs", "previs", "check_spec", "geometry", "framesheets",
          "previs_review", "compile", "publish", "report"]
SEASON_STAGES = ["confirm", "assets", "images", "asset_review", "sound"]  # 剧集模式：剧目录只出全季资产，其余逐集跑（docs/22）
sys.path.insert(0, TOOLS)


class Stop(Exception):
    """阶段没过：带一句 NEXT 指令。"""

    def __init__(self, msg, next_step):
        super().__init__(msg)
        self.next_step = next_step


# ---------------- 状态 ----------------

def pj(proj):
    return os.path.join(proj, "production.json")


MODEL_ROUTE = {  # 每家的路由要点（docs/13）；验证状态如实写
    "seedance": "Seedance 2.5 全能参考，白模作 @视频1，中文时间轴；时长 4–30 整秒；已实拍（《天劫外包》第 1 条）。",
    "hailuo": "海螺 H3 Ref2VA，官方英文六段式，白模是 <Video 1> 的 weak_reference，人物是 <Subject N>；单段 4–15 秒、参考图 ≤ 9；未实拍，先出 768P 样片（或本地部署）。",
    "wan": "万相 3.0 参考生视频，默认只传图、不喂白模（2026-11-09 用户决定）：每个分镜写景别、焦段、俯仰、一个运镜、谁在画面左右、姿势，动作写整段的绝对秒数，台词写\"图N（某某）说\"；单条 2–30 秒、参考图 ≤ 10、prompt_extend 关；production.json 写 \"wan_previs\": true 才喂白模（单段 ≤ 15 秒，白模秒数也计费）；未实拍，先出 480P 样片。",
}


def load(proj):
    p = pj(proj)
    d = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    d.setdefault("confirm", {}).setdefault("story", False)
    d["confirm"].setdefault("script", False)
    d.setdefault("budget", {}).setdefault("images", {"approved": 0, "used": 0})
    d.setdefault("video_models", ["seedance"])
    d.setdefault("pipeline", {})
    d.setdefault("reviews", {})
    return d


def save(proj, d):
    json.dump(d, open(pj(proj), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def sha(*paths):
    h = hashlib.sha256()
    for p in paths:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.endswith(".json"):
                    h.update(open(os.path.join(p, f), "rb").read())
        elif os.path.exists(p):
            h.update(open(p, "rb").read())
    return h.hexdigest()[:16]


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Stop(f"命令失败：{' '.join(os.path.basename(c) for c in cmd[:3])}\n{(r.stderr or r.stdout)[-1500:]}",
                   "看上面的报错修好脚本，再跑 run")
    return r


def mark(d, stage, **kw):
    d["pipeline"][stage] = dict(kw, done=now())


def clips(proj):
    sd = os.path.join(proj, "交付", "规格单")
    return sorted((f[:-5] for f in os.listdir(sd) if f.endswith(".json")), key=lambda x: [int(n) for n in re.findall(r"\d+", x)]) \
        if os.path.isdir(sd) else []


def asset_table(proj):
    import image_gen
    return image_gen.parse_doc(os.path.join(proj, "交付", "01-资产提示词.md"))


# ---------------- 各阶段 ----------------

def skind(proj):
    import storyboard
    return storyboard.series_kind(proj)


def json_mode(proj):
    return os.path.exists(os.path.join(proj, "设定.json")) or skind(proj) == "episode"


def offline(d):
    return bool(d.get("selftest"))


def st_confirm(proj, d):
    if skind(proj) == "episode":
        root = os.path.dirname(proj)
        sd = load(root)
        if not (sd["confirm"]["story"] and sd["confirm"]["script"]):
            raise Stop("全季的故事（季圣经）和设定还没确认", f"用户确认后跑 `pipeline.py confirm {os.path.relpath(root, ROOT)} story` 和 `script`")
        if not d["confirm"]["script"]:
            raise Stop(f"第 {os.path.basename(proj)[2:]} 集剧本还没确认",
                       f"把这一集的剧本发给用户确认，确认后跑 `pipeline.py confirm {os.path.relpath(proj, ROOT)} script`")
        if not os.path.exists(os.path.join(proj, "分镜.json")):
            raise Stop("缺少 分镜.json", "按 docs/19、docs/22 把这一集剧本拆成 分镜.json（设定用剧目录的），`storyboard.py check` 通过后再 run")
        mark(d, "confirm")
        return
    if skind(proj) == "season" and not os.path.exists(os.path.join(proj, "季.json")):
        raise Stop("缺少 季.json", "按 docs/22 写全季的 季.json")
    miss = [k for k, n in (("story", "故事"), ("script", "剧本")) if not d["confirm"][k]]
    if miss:
        raise Stop("用户还没确认：" + "、".join(miss),
                   "把故事 / 剧本发给用户确认；用户在对话里明确确认后，跑 `pipeline.py confirm <项目> story` 或 `script`")
    need = ("设定.json", "季.json") if skind(proj) == "season" else ("设定.json", "分镜.json") if json_mode(proj) else ("预演规格/build_assets.py", "预演规格/gen_clips.py")
    for f in need:
        if not os.path.exists(os.path.join(proj, f)):
            raise Stop(f"缺少 {f}", "按 docs/19 把剧本拆成 设定.json 和 分镜.json（样例 examples/样例-便利店/），"
                                     "写完跑 `python3 tools/storyboard.py check <项目>` 直到通过")
    mark(d, "confirm")


def build(proj, what):
    if json_mode(proj):
        r = run([PY, "-I", os.path.join(TOOLS, "storyboard.py"), what, proj], check=False)
        if r.returncode != 0:
            raise Stop("设定.json / 分镜.json 校验没过：\n" + "\n".join(l for l in r.stdout.splitlines() if l.startswith("ERROR"))[:3000],
                       "按每条 ERROR 后面的说明改 设定.json / 分镜.json，`python3 tools/storyboard.py check <项目>` 通过后再 run")
        return
    src = os.path.join(proj, "预演规格", "build_assets.py" if what == "assets" else "gen_clips.py")
    run([PY, "-I", src], cwd=os.path.dirname(src))


def _season(fn):
    """剧集模式：资产三阶段在剧目录上跑（全季共用一套资产，预算和目检记在剧目录），跑完把定版图和资产文档同步进这一集。"""
    def wrap(proj, d):
        if skind(proj) != "episode":
            return fn(proj, d)
        root = os.path.dirname(proj)
        sd = load(root)
        try:
            fn(root, sd)
        except Stop as e:
            save(root, sd)
            raise Stop(f"（全季资产，剧目录 {os.path.relpath(root, ROOT)}）{e}", e.next_step.replace("<项目>", os.path.relpath(root, ROOT)))
        save(root, sd)
        if fn is st_sound:
            src, dst = os.path.join(root, "交付", "声音"), os.path.join(proj, "交付", "声音")
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for f in os.listdir(src):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
        if fn is st_asset_review:
            src, dst = os.path.join(root, "交付", "参考图定版"), os.path.join(proj, "交付", "参考图定版")
            os.makedirs(dst, exist_ok=True)
            for f in os.listdir(src):
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
            shutil.copy2(os.path.join(root, "交付", "01-资产提示词.md"), os.path.join(proj, "交付", "01-资产提示词.md"))
        mark(d, fn.__name__[3:], season=True)
    return wrap


def st_assets(proj, d):
    build(proj, "assets")
    table = asset_table(proj)
    errs = []
    for n, it in table.items():
        if "text" not in it and "banana" not in it:
            errs.append(f"{n} {it['tag']} 没有提示词代码块")
        if "text" in it and "STYLE:" not in it["text"]:
            errs.append(f"{n} {it['tag']} 没有 STYLE 段（docs/16）")
        if re.search(r"grain|pores|blemish|halation", it.get("text", ""), re.I):
            errs.append(f"{n} {it['tag']} 写了颗粒 / 毛孔词，Image 2.5 会照字面加噪点（docs/16 第二节）")
        for dep in it["deps"]:
            if dep not in table or dep >= n:
                errs.append(f"{n} {it['tag']} 依赖 {dep} 不存在或排在后面")
    if errs:
        raise Stop("资产文档不合格：\n  " + "\n  ".join(errs), "改 预演规格/build_assets.py 后重跑 run")
    mark(d, "assets", count=len(table), hash=sha(os.path.join(proj, "交付", "01-资产提示词.md")))


def st_images(proj, d):
    table = asset_table(proj)
    out = os.path.join(proj, "交付", "资产图")
    os.makedirs(out, exist_ok=True)
    missing = [n for n, it in table.items() if not os.path.exists(os.path.join(out, it["file"]))]
    b = d["budget"]["images"]
    if missing and offline(d):  # 自测：不花钱，用占位图
        from PIL import Image, ImageDraw
        for n in missing:
            im = Image.new("RGB", (2048, 1152), (90, 90, 96))
            ImageDraw.Draw(im).text((40, 40), f"PLACEHOLDER {n}", fill=(255, 255, 255))
            im.save(os.path.join(out, table[n]["file"]))
        missing = []
    if missing:
        left = b["approved"] - b["used"]
        if left < len(missing):
            raise Stop(f"还缺 {len(missing)} 张图（序号 {missing}），出图预算剩 {left} 张",
                       f"告诉用户要出 {len(missing)} 张、走哪个接口、大约多少钱（`python3 -I tools/image_gen.py <资产文档> --out <资产图> --dry-run`）；"
                       f"用户明确同意后跑 `pipeline.py approve <项目> images {len(missing)}`")
        r = run([PY, os.path.join(TOOLS, "image_gen.py"), os.path.join(proj, "交付", "01-资产提示词.md"), "--out", out,
                 "--only", ",".join(map(str, missing))], check=False)
        open(os.path.join(proj, "logs", f"出图_{datetime.date.today()}.txt"), "a", encoding="utf-8").write(r.stdout + r.stderr)
        made = [n for n in missing if os.path.exists(os.path.join(out, table[n]["file"]))]
        b["used"] += len(made)
        if len(made) < len(missing):
            save(proj, d)
            raise Stop(f"出图失败 {sorted(set(missing) - set(made))}，见 logs/出图_*.txt", "看日志排查接口或提示词，修好后重跑 run")
    mark(d, "images", count=len(table))


def _img_key(it):
    return "图:" + it["file"]


def st_asset_review(proj, d):
    table = asset_table(proj)
    out = os.path.join(proj, "交付", "资产图")
    final = os.path.join(proj, "交付", "参考图定版")
    os.makedirs(final, exist_ok=True)
    pending, fixes = [], []
    for n, it in table.items():
        p = os.path.join(out, it["file"])
        k, h = _img_key(it), sha(p)
        rv = d["reviews"].get(k)
        if not rv or rv.get("hash") != h:
            d["reviews"][k] = {"status": "pass" if offline(d) else "待目检", "hash": h, "n": n, "check": it.get("check", "")}
            rv = d["reviews"][k]
        if rv["status"] == "待目检":
            pending.append((n, it))
        elif rv["status"] == "fix":
            fixes.append((n, it, rv.get("note", "")))
        else:
            shutil.copy2(p, os.path.join(final, it["file"]))
    if pending:
        sheet = os.path.join(proj, "logs", "待目检_资产.jpg")
        contact_sheet([os.path.join(out, it["file"]) for _, it in pending], [f"{n} {it['tag']}" for n, it in pending], sheet)
        raise Stop(f"{len(pending)} 张图待目检：" + "、".join(f"{n} {it['tag']}" for n, it in pending),
                   f"逐张打开原图（拼图在 logs/待目检_资产.jpg），对照资产文档里该图的「出图后检查」和描述锁看；合格跑 "
                   f"`pipeline.py review <项目> 图:<文件名> pass`；小出入按 docs/14 第四节第 4 步改描述锁跟图后再 pass；"
                   f"不合格跑 `review ... fix <原因>`，改提示词后 `pipeline.py redo <项目> <序号>`")
    if fixes:
        raise Stop("有图判了 fix：" + "；".join(f"{n} {it['tag']}（{note}）" for n, it, note in fixes),
                   "改 build_assets.py 里这张图的提示词，然后 `pipeline.py redo <项目> <序号>`，再 run（会占出图预算）")
    mark(d, "asset_review")


def audio_stats(path):
    """时长、平均响度、峰值（ffmpeg volumedetect）。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    mean = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    peak = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
    sec = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0.0
    return sec, float(mean.group(1)) if mean else -99.0, float(peak.group(1)) if peak else -99.0


def st_sound(proj, d):
    """声音资产（crew/09）：开口角色的音色参考（喂视频模型）+ 场景环境底 / 配乐（后期铺底）。本机免费生成，流水线只做技术检查。
    production.json 写 "sound_assets": false 可关掉。"""
    if d.get("sound_assets") is False or not json_mode(proj):
        mark(d, "sound", skipped=True)
        return
    run([PY, "-I", os.path.join(TOOLS, "storyboard.py"), "sound", proj])
    items = json.load(open(os.path.join(proj, "交付", "声音清单.json"), encoding="utf-8"))
    base = os.path.join(proj, "交付")
    os.makedirs(os.path.join(base, "声音"), exist_ok=True)
    missing = [it for it in items if not os.path.exists(os.path.join(base, it["file"]))]
    if missing and offline(d):  # 自测：固定的占位音（音色 6 秒，其余取下限）
        for it in missing:
            sec = 6 if it["kind"] == "voice" else it["dur"][0]
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={sec}", "-ar", "48000",
                            "-af", "volume=-12dB", os.path.join(base, it["file"])], check=True)
        missing = []
    user_v = [it for it in missing if it["kind"] == "voice" and not it["tool"].startswith("local-tts")]
    if user_v:
        raise Stop("voice_refs 设成了 user，还缺真人录音：" + "、".join(it["file"] for it in user_v),
                   "请用户提供这几个角色的真人录音（5–30 秒，正常说话、干净、没有配乐，授权可用），放到对应路径后重跑 run；"
                   "不想用了就把 production.json 的 voice_refs 改回 off")
    if missing:
        raise Stop(f"还缺 {len(missing)} 段声音：" + "、".join(it["file"] for it in missing),
                   "按 交付/02-声音资产.md 每一项的工具和参数生成（本机免费，不占预算）：环境底和配乐用 audio-studio 的 generate_audio"
                   "（再用 process_audio 转 48 kHz、环境底做成无缝循环）；voice_refs 是 tts 时音色参考用 local-tts 的 design_voice；"
                   "存到表里的路径后重跑 run。告诉用户：这些声音没人听过，请试听，不满意就 `review <项目> 声:<文件名> fix <原因>`")
    bad = []
    for it in items:
        p = os.path.join(base, it["file"])
        sec, mean, peak = audio_stats(p)
        lo, hi = it["dur"]
        if not lo <= sec <= hi:
            bad.append(f"{it['file']} 时长 {sec:.1f} 秒，要 {lo}–{hi} 秒")
        if mean < -40:
            bad.append(f"{it['file']} 平均响度 {mean:.0f} dB，几乎没声音")
        if peak > -0.1:
            bad.append(f"{it['file']} 峰值 {peak:.1f} dB，削波")
        k = "声:" + os.path.basename(it["file"])
        h = sha(p)
        if d["reviews"].get(k, {}).get("hash") != h:
            d["reviews"][k] = {"status": "pass" if offline(d) else "未试听", "hash": h}
        if d["reviews"][k]["status"] == "fix":
            bad.append(f"{it['file']} 判了 fix：{d['reviews'][k].get('note', '')}")
    if bad:
        raise Stop("声音资产不合格：\n  " + "\n  ".join(bad), "按提示重生成或用 process_audio 裁剪、归一化后覆盖原文件，再 run（判 fix 的改好后 `review <项目> 声:<文件名> pass`）")
    mark(d, "sound", items=len(items), unheard=[k for k, v in d["reviews"].items() if k.startswith("声:") and v["status"] == "未试听"])


def st_stress(proj, d):
    """可选：角色压力测试（production.json 里 "stress_test": true 才做；crew/07，定稿前看真人感和一致性）。"""
    if not d.get("stress_test") or not json_mode(proj) or skind(proj):
        mark(d, "stress", skipped=True)
        return
    run([PY, "-I", os.path.join(TOOLS, "storyboard.py"), "stress", proj])
    import image_gen
    doc = os.path.join(proj, "交付", "01b-角色压力测试.md")
    table = image_gen.parse_doc(doc)
    out = os.path.join(proj, "交付", "资产图")
    tests = {n: it for n, it in table.items() if it["type"] == "压力测试"}
    missing = [n for n, it in tests.items() if not os.path.exists(os.path.join(out, it["file"]))]
    b = d["budget"]["images"]
    if missing and not offline(d):
        if b["approved"] - b["used"] < len(missing):
            raise Stop(f"角色压力测试要出 {len(missing)} 张，出图预算不够",
                       f"告诉用户压力测试要出 {len(missing)} 张（每个主角 3 张，看侧脸、暗处、同框时还像不像真人、像不像同一个人）；同意后 `pipeline.py approve <项目> images {len(missing)}`")
        r = run([PY, os.path.join(TOOLS, "image_gen.py"), doc, "--out", out, "--only", ",".join(map(str, missing))], check=False)
        open(os.path.join(proj, "logs", f"出图_{datetime.date.today()}.txt"), "a", encoding="utf-8").write(r.stdout + r.stderr)
        b["used"] += sum(1 for n in missing if os.path.exists(os.path.join(out, tests[n]["file"])))
    pend, fix = [], []
    for n, it in tests.items():
        p = os.path.join(out, it["file"])
        if not os.path.exists(p):
            if offline(d):
                continue
            raise Stop(f"压力测试图 {it['file']} 没出来", "看 logs/出图_*.txt，修好后重跑 run")
        k, h = "压测:" + it["file"], sha(p)
        if d["reviews"].get(k, {}).get("hash") != h:
            d["reviews"][k] = {"status": "pass" if offline(d) else "待目检", "hash": h}
        st = d["reviews"][k]["status"]
        (pend if st == "待目检" else fix if st == "fix" else []).append((n, it))
    if pend:
        contact_sheet([os.path.join(out, it["file"]) for _, it in pend], [f"{it['tag']} {it['use']}" for _, it in pend],
                      os.path.join(proj, "logs", "待目检_压力测试.jpg"))
        raise Stop("角色压力测试待目检：" + "、".join(it["tag"] for _, it in pend),
                   "打开 logs/待目检_压力测试.jpg 和原图：同一个人吗？侧脸、暗处、同框时还像真人吗？剪影认得出吗？合格 `review <项目> 压测:<文件名> pass`；"
                   "不合格 `review … fix <原因>`，改人物描述锁或重出三视图，再删掉对应压测图重跑")
    if fix:
        raise Stop("压力测试有 fix：" + "、".join(it["tag"] for _, it in fix), "改人物描述锁 / 三视图（必要时 redo 三视图），删掉对应压测图后重跑 run")
    mark(d, "stress", tests=len(tests))


def st_specs(proj, d):
    build(proj, "specs")
    if not clips(proj):
        raise Stop("没有导出任何规格单", "检查 分镜.json 的 clips（或 gen_clips.py 的 CLIPS）")
    mark(d, "specs", clips=clips(proj))


def st_check_spec(proj, d):
    r = run([PY, "-I", os.path.join(TOOLS, "compile", "check_spec.py"), os.path.join(proj, "交付", "规格单")], check=False)
    open(os.path.join(proj, "logs", "规格自检.txt"), "w", encoding="utf-8").write(r.stdout)
    if "FAIL" in r.stdout:
        raise Stop("规格自检有 FAIL：\n" + "\n".join(l for l in r.stdout.splitlines() if "FAIL" in l)[:2000],
                   "按提示改 gen_clips.py（时长、画面里、台词速度、说话人入画），再 run")
    mark(d, "check_spec")


def _clip_files(proj, c):
    spec = json.load(open(os.path.join(proj, "交付", "规格单", c + ".json"), encoding="utf-8"))
    prev_spec = os.path.normpath(os.path.join(proj, "交付", spec["previs_spec"]))  # 路径相对 交付/（与 compile_video 一致）
    mp4 = os.path.join(proj, "交付", spec["previs"])
    return spec, prev_spec, mp4


PREVIS_CODE = [os.path.join(TOOLS, "previs", f) for f in ("build_from_spec.py", "common.py")]  # 白模代码改了（光、巨物代理……）也要重渲


def st_previs(proj, d):
    hashes = d["pipeline"].get("previs", {}).get("hashes", {})
    done = []
    for c in clips(proj):
        spec, prev_spec, mp4 = _clip_files(proj, c)
        h = sha(prev_spec, *PREVIS_CODE)
        if os.path.exists(mp4) and hashes.get(c) == h:
            continue
        run([PY, os.path.join(TOOLS, "previs", "render_previs.py"), prev_spec, "--out", os.path.dirname(mp4)])
        hashes[c] = h
        done.append(c)
    mark(d, "previs", hashes=hashes, rendered=done)


def st_geometry(proj, d):
    hashes = d["pipeline"].get("geometry", {}).get("hashes", {})
    bad = []
    for c in clips(proj):
        spec, prev_spec, mp4 = _clip_files(proj, c)
        sp = os.path.join(proj, "交付", "规格单", c + ".json")
        h = sha(prev_spec, sp, *PREVIS_CODE, os.path.join(TOOLS, "previs", "geometry_check.py"))
        if hashes.get(c) == h:
            continue
        rep = os.path.join(proj, "logs", f"几何检查_{c}.md")
        r = run([PY, os.path.join(TOOLS, "previs", "geometry_check.py"), prev_spec, sp, "--out", rep], check=False)
        txt = open(rep, encoding="utf-8").read() if os.path.exists(rep) else ""
        if r.returncode != 0 or "| FAIL |" in txt or "| WARN |" in txt:  # WARN（遮挡、太小、头后竖直物、越轴）也拦：零抽卡
            bad.append(c)
        else:
            hashes[c] = h
    d["pipeline"]["geometry"] = {"hashes": hashes, "done": now()}
    if bad:
        raise Stop("白模几何检查有 FAIL / WARN：" + "、".join(bad),
                   "看 logs/几何检查_第N条.md：改 分镜.json 里的站位（cast）、景别（size）、机位（view / cam）或场景 stage 的 marks / views（脚本模式改 gen_clips.py）；确属有意（如前景遮挡）就在该镜写 allow_occlude / allow_small，再 run")


def contact_sheet(paths, labels, out, cols=4, w=640, h=360):
    from PIL import Image, ImageDraw, ImageFont
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (w * min(cols, len(paths)), h * rows))
    dr = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 24)
    except OSError:
        font = ImageFont.load_default()
    for i, (p, lab) in enumerate(zip(paths, labels)):
        im = Image.open(p).convert("RGB")
        im.thumbnail((w, h))
        x, y = (i % cols) * w, (i // cols) * h
        sheet.paste(im, (x + (w - im.width) // 2, y + (h - im.height) // 2))
        dr.rectangle([x, y, x + w, y + 32], fill=(0, 0, 0))
        dr.text((x + 6, y + 3), lab, fill=(255, 220, 0), font=font)
    sheet.save(out, quality=85)


def st_framesheets(proj, d):
    hashes = d["pipeline"].get("framesheets", {}).get("hashes", {})
    for c in clips(proj):
        spec, prev_spec, mp4 = _clip_files(proj, c)
        h = sha(mp4)
        out = os.path.join(proj, "logs", f"白模拼图_{c}.jpg")
        k = "白模:" + c
        if hashes.get(c) == h and os.path.exists(out):
            continue
        run(["ffmpeg", "-v", "error", "-y", "-ss", "0.25", "-i", mp4, "-vf", "fps=2,scale=320:-1,tile=10x6", "-frames:v", "1", out])
        hashes[c] = h
        if d["reviews"].get(k, {}).get("hash") != h:
            d["reviews"][k] = {"status": "pass" if offline(d) else "待目检", "hash": h}
    problems = [x for c in clips(proj) for x in frame_audit(proj, c)]
    if d.get("first_frames", True):
        first_frames(proj)
    bad = [x for x in problems if x.startswith("ERROR")]
    if bad and not offline(d):
        raise Stop("白模逐帧检查没过：\n" + "\n".join(bad), "空白画面：给这一镜补道具（prop_states / beats）、换机位，或确属有意就在这一镜写 allow_blank: true；"
                   "节拍看不出动作：检查节拍的 who / target 是否在画面里、动作幅度够不够；改完再 run（拼图在 logs/白模逐帧_第N条.jpg）")
    mark(d, "framesheets", hashes=hashes, notes=problems)


def frame_audit(proj, c):
    """精准白模的机检（4.0 alpha.8）：每秒 4 帧抽白模，
    1) 每镜中段近乎纯色 → 空白画面（ERROR，除非 allow_blank）；
    2) 每个节拍发生后 0.6 秒内画面几乎没变 → 看不出动作（WARN）；
    3) 出一张逐帧拼图 logs/白模逐帧_第N条.jpg：一行一镜，标镜头号，节拍那一帧加红框。"""
    from PIL import Image, ImageDraw, ImageFont, ImageStat, ImageChops
    spec, _, mp4 = _clip_files(proj, c)
    if not os.path.exists(mp4):
        return []
    tmp = os.path.join(proj, "logs", "_frames_" + c)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-vf", "fps=4,scale=240:-2", os.path.join(tmp, "f_%04d.png")])
    files = sorted(os.listdir(tmp))
    frames = [Image.open(os.path.join(tmp, f)).convert("RGB") for f in files]
    for f in files:
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)
    if not frames:
        return []
    fr = lambda t: frames[min(len(frames) - 1, max(0, int(t * 4)))]  # noqa: E731
    board = {str(x["n"]): x for x in json.load(open(os.path.join(proj, "分镜.json"), encoding="utf-8"))["clips"]} if os.path.exists(os.path.join(proj, "分镜.json")) else {}
    raw = board.get(c.replace("第", "").replace("条", ""), {}).get("shots", [])
    out, rows = [], []
    for k, sh in enumerate(spec["shots"]):
        t0, t1 = sh["t0"], sh["t1"]
        src = raw[k] if k < len(raw) else {}
        sds = [ImageStat.Stat(fr(t0 + (t1 - t0) * q).convert("L")).stddev[0] for q in (0.3, 0.5, 0.7)]
        if max(sds) < 6 and not src.get("allow_blank"):
            out.append(f"ERROR {c} 镜{sh['id']} {sh['name']}：画面几乎是纯色（{max(sds):.1f}），白模里这一镜什么都没拍到")
        beat_ts = []
        for bt in src.get("beats", []):
            ta = t0 + bt["t"]
            beat_ts.append(ta)
            if not (bt.get("text") or bt.get("prop")) or bt.get("do") == "hide" or bt.get("unseen"):
                continue  # 只查写进提示词的节拍和道具（没写 text 的坐着、扶着是背景表演）
            a, b = fr(max(t0, ta - 0.1)).convert("L"), fr(min(t1 - 0.05, ta + 0.8)).convert("L")
            moved = sum(1 for v in ImageChops.difference(a, b).getdata() if v > 25)  # 变了的像素数：远景里的小人跑动也算得出来
            if moved < 30:
                who = bt.get("who") or bt.get("prop")
                out.append(f"WARN  {c} 镜{sh['id']} 第 {bt['t']:g} 秒「{who} {bt.get('do')}」：前后 0.9 秒只有 {moved} 个像素在变，白模里看不出这个动作")
        rows.append((sh, [(t, fr(t)) for t in [t0 + 0.125 + i * 0.25 for i in range(int((t1 - t0) * 4))]], beat_ts))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 18)
    except Exception:
        font = ImageFont.load_default()
    W, H = frames[0].size
    per = 10
    lines = sum(max(1, (len(f) + per - 1) // per) for _, f, _ in rows)
    sheet = Image.new("RGB", (W * per + 120, lines * (H + 4) + 4), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    y = 4
    for sh, fs, beats in rows:
        d.text((6, y + 4), f"镜{sh['id']}", fill=(240, 240, 240), font=font)
        d.text((6, y + 28), sh["name"], fill=(200, 200, 200), font=font)
        for i, (t, im) in enumerate(fs):
            x = 120 + (i % per) * W
            yy = y + (i // per) * (H + 4)
            sheet.paste(im, (x, yy))
            if any(abs(t - bt) <= 0.125 for bt in beats):
                d.rectangle([x, yy, x + W - 1, yy + H - 1], outline=(230, 40, 40), width=3)
        y += max(1, (len(fs) + per - 1) // per) * (H + 4)
    sheet.save(os.path.join(proj, "logs", f"白模逐帧_{c}.jpg"), quality=85)
    return out


FF_RISK = re.compile(r"雨|湿|帽|纹身|眼镜|首饰|项链|耳环|戒指|文字|招牌|屏幕|镜子|面具|印章|徽章")


def first_frames(proj):
    """首帧图（docs/14 第五节）：只给高风险的条——开场镜头里有雨、帽子、纹身、小道具、文字、镜子这类模型最容易画错的东西，
    或这一条要接上一条的戏——写一张首帧图的出图提示词，构图取白模第一帧。出图要用户点头；定稿存 交付/首帧图/第N条.png，重跑自动进投喂包。"""
    out_dir = os.path.join(proj, "交付", "首帧图")
    rows = []
    for c in clips(proj):
        spec, _, mp4 = _clip_files(proj, c)
        sh = spec["shots"][0]
        body = re.sub(r"<[^>]*>", "", sh["body"])
        why = sorted(set(FF_RISK.findall(body)))
        reasons = ([f"开场镜头里有{'、'.join(why)}"] if why else []) + ([f"接第{spec['splice_in']['from']}条的戏"] if spec.get("splice_in") else [])
        if not reasons:
            continue
        os.makedirs(out_dir, exist_ok=True)
        frame = os.path.join(out_dir, f"{c}_白模构图.jpg")
        if os.path.exists(mp4):
            run(["ffmpeg", "-v", "error", "-y", "-ss", "0.04", "-i", mp4, "-frames:v", "1", "-q:v", "3", frame], check=False)
        names = {f"@图片{i}": a.get("name") or a["tag"].lstrip("@") for i, a in enumerate(spec["assets"], 1)}
        body = re.sub(r"@图片\d+", lambda m: names.get(m.group(0), m.group(0)), body)
        nm = lambda a: (a.get("name") or a["tag"].lstrip("@")).replace("prop_", "")  # noqa: E731
        refs = [a for a in spec["assets"] if a["kind"] == "scene" or nm(a) in body or (a["kind"] == "prop" and nm(a)[-2:] in body)]
        still = body.split("；")[0].rstrip("。") + "。"  # 只要"画面里 + 机位"：首帧是动作开始之前的那一瞬间
        ref_lines = ["参考图1：`交付/首帧图/" + os.path.basename(frame) + "`（白模构图）"] + [
            f"参考图{i}：`交付/{a['file']}`（{a.get('name') or a['tag'].lstrip('@')}）" for i, a in enumerate(refs, 2)]
        if spec.get("splice_in"):
            ref_lines.append(f"参考图{len(refs) + 2}：`交付/{spec['splice_in']['file']}`（第{spec['splice_in']['from']}条出片后的尾帧，出完那条才有）")
        prompt = (f"一张 {spec.get('ratio', '16:9')} 的电影画面，作为一段视频的第一帧，取这一镜开始的那一瞬间。"
                  f"机位、景别、构图和人物位置完全照参考图1：灰色圆柱是人物的占位、方块是场景里的物件，只取位置，不画出白模本身。"
                  f"{still}这一镜的动作还没开始。"
                  + (f"人物的位置、姿态、服装状态和光，和最后一张参考图（上一条的尾帧）一致。" if spec.get("splice_in") else "")
                  + f"人物、道具、场景的外观照后面的参考图，不继承参考图的灰底和版式。{spec['style']}画面里不出现可读的文字。")
        rows += [f"## {c} · {spec['title']}", "", f"- 为什么要首帧图：{'；'.join(reasons)}", "- 参考图：" + "；".join(ref_lines),
                 "- 推荐出图：Image 2.5（APIMart 2K，带参考图约 $0.045 / 张，docs/10）", f"- 定稿后存为 `交付/首帧图/{c}.png`，再 `run` 一次就会接进这一条的投喂包（只进第一段）。",
                 "", "```", prompt, "```", ""]
    doc = os.path.join(proj, "交付", "03-首帧图提示词.md")
    if rows:
        n = sum(1 for r in rows if r.startswith("## "))
        open(doc, "w", encoding="utf-8").write("\n".join([
            "# 首帧图提示词（可选）", "",
            f"> 共 {n} 条值得加首帧图：开场镜头里有模型最容易画错的东西，或要接上一条的戏。只有一条视频的**第一帧**能用首帧图锁住；"
            "其余镜头仍只靠白模和资产图。出图要先报张数和花费、用户点头（docs/09 第三个确认点）。目检通过的图存成 `交付/首帧图/第N条.png`。", ""] + rows))
    elif os.path.exists(doc):
        os.remove(doc)


def st_previs_review(proj, d):
    pending = [c for c in clips(proj) if d["reviews"].get("白模:" + c, {}).get("status") != "pass"]
    if pending:
        raise Stop("白模待目检：" + "、".join(pending),
                   "逐条打开 logs/白模拼图_第N条.jpg（每 0.5 秒一帧），对照规格单每镜的「画面里」：主体都在画里、前景过肩人物入画、"
                   "没人挡住说话人、手部和物件特写拍对了东西；合格跑 `pipeline.py review <项目> 白模:第N条 pass`，"
                   "不合格改 gen_clips.py 再 run（会重渲那一条）")
    mark(d, "previs_review")


def _pack_hashes(proj):
    """每条投喂包的指纹（提示词 + 白模），用来发现"已经出过片的条，投喂包又变了"。"""
    out = {}
    base = os.path.join(proj, "交付", "投喂包")
    if not os.path.isdir(base):
        return out
    for m in sorted(os.listdir(base)):
        md = os.path.join(base, m)
        if not os.path.isdir(md):
            continue
        for c in sorted(os.listdir(md)):
            h = hashlib.sha256()
            for f in ("提示词.txt", "视频1_白模.mp4"):
                p = os.path.join(md, c, f)
                if os.path.exists(p):
                    h.update(open(p, "rb").read())
            out[f"{m}/{c}"] = h.hexdigest()[:12]
    return out


def st_compile(proj, d):
    fails, waits = [], []
    prev = (d["pipeline"].get("compile") or {}).get("packs") or d.get("packs_last") or {}
    if d.get("seedance_api_model"):  # 例：doubao-seedance-2-0-mini-260615（便宜档，docs/13 第八·四节）
        os.environ["SEEDANCE_API_MODEL"] = d["seedance_api_model"]
    os.environ["WAN_PREVIS"] = "1" if d.get("wan_previs") else "0"  # 万相默认不喂白模（2026-11-09 用户决定）
    for m in d["video_models"]:
        r = run([PY, "-I", os.path.join(TOOLS, "compile", "compile_video.py"), os.path.join(proj, "交付", "规格单"), "--model", m], check=False)
        fails += [l for l in r.stdout.splitlines() if l.startswith("FAIL")]
    for root, _, files in os.walk(os.path.join(proj, "交付", "投喂包")):
        if "检查.txt" in files:
            waits += [os.path.relpath(root, proj) + "：" + l for l in open(os.path.join(root, "检查.txt"), encoding="utf-8") if l.startswith("WAIT")]
    packs = _pack_hashes(proj)
    changed = sorted(k for k, v in packs.items() if prev and prev.get(k) != v)
    d["packs_last"] = packs
    d["pipeline"]["compile"] = {"models": d["video_models"], "fails": fails, "waits": [w.strip() for w in waits], "packs": packs,
                                "changed": changed, "done": now()}
    if waits:
        raise Stop("投喂包还缺定稿图：\n" + "\n".join(waits)[:1500], "回到出图：确认资产都过了目检（status 看 asset_review）")
    if fails:
        known = d.get("known_fails", [])
        new = [f for f in fails if not any(k in f for k in known)]
        if new:
            raise Stop("编译有 FAIL：\n" + "\n".join(new), "按 FAIL 说明拆条或换模型；确实不提交的段落写进 production.json 的 known_fails 并告诉用户")


def st_publish(proj, d):
    """剧集模式：每集一份发布包（封面提示词、下集预告、文案、合规）；单片跳过（docs/22）。"""
    if skind(proj) != "episode":
        mark(d, "publish", skipped=True)
        return
    import storyboard
    root, ep = os.path.dirname(proj), os.path.basename(proj)[2:]
    bible, board = storyboard.load(proj)
    season = board["_series"]["season"]
    e = next(x for x in season["episodes"] if x["ep"] == ep)
    miss = [k for k in ("titles", "blurb", "tags") if not e.get(k)]
    if miss:
        raise Stop(f"季.json 第 {ep} 集缺发布文案：{'、'.join(miss)}",
                   "在 季.json 这一集写 titles（3 个钩子标题）、blurb（80–120 字简介）、tags（5–8 个话题），可选 pinned（置顶评论），再 run")
    clips_ = board.get("clips", [])
    pool = [c for c in clips_ if not c.get("cold_open_of")] or clips_  # 封面不用冷开场
    top = max(pool, key=lambda c: ((c.get("energy") or {}).get("out", 0), (c.get("energy") or {}).get("in", 0)))
    spec = json.load(open(os.path.join(proj, "交付", "规格单", f"第{top['n']}条.json"), encoding="utf-8"))
    sig = (top.get("signature") or {}).get("shot", 1)
    body = spec["shots"][sig - 1]["body"]
    refs = "、".join(a["tag"] for a in spec["assets"] if not a["tag"].startswith("@crowd"))
    style = spec.get("style_en", "")
    scene = re.sub(r"；[^；。]*：\{[^}]*\}|<[^>]*>|\{[^}]*\}", "", body).strip("；; ")  # 封面是静帧：去掉台词和声音
    cover = lambda r: (f"A {r} cover still for an AI short drama episode, near live-action cinematic. Scene (keep exactly): {scene} "  # noqa: E731
                       f"The main character faces the camera in the upper-middle of the frame; leave clear space in the top third for a title added later. "
                       f"{style} No text, letters, numbers or watermarks.")
    nxt = [x["ep"] for x in season["episodes"]]
    i = nxt.index(ep)
    teaser = ["（最后一集）"]
    if i + 1 < len(nxt):
        np_ = os.path.join(root, f"ep{nxt[i + 1]}", "分镜.json")
        if os.path.exists(np_):
            nb = json.load(open(np_, encoding="utf-8"))
            best = sorted([c for c in nb.get("clips", []) if not c.get("cold_open_of")], key=lambda c: -(c.get("energy") or {}).get("out", 0))[:3]
            teaser = [f"- 第 {nxt[i + 1]} 集第{c['n']}条 第 {(c.get('signature') or {}).get('shot', 1)} 镜：{(c.get('signature') or {}).get('why', '')}" for c in best]
            line = next((s["line"]["text"] for c in best for s in c.get("shots", []) if s.get("line")), "")
            if line:
                teaser.append(f"- 配一句台词：「{line}」")
        else:
            teaser = [f"（第 {nxt[i + 1]} 集分镜写完后重跑 run 生成）"]
    L = [f"# 发布包：{season.get('title', bible.get('title', ''))} 第 {ep} 集《{e.get('title', '')}》", "",
         "## 封面（出图前报张数和花费）", "", f"参考图：{refs}（取自 交付/参考图定版/）", "",
         "竖版 3:4：", "", "```text", cover("vertical 3:4"), "```", "", "横版 4:3：", "", "```text", cover("horizontal 4:3"), "```", "",
         "## 片尾与下集预告（接在正片之后，不占正片时长）", "", "- 片尾卡 3 秒：本集结尾钩子定格 + 剧名。", f"- 结尾钩子：{board.get('hook', '')}",
         "- 下集预告 8–10 秒，取下一集最强的 2–3 个镜头：", *teaser, "",
         "## 文案", "", "钩子标题：", *[f"- {t}" for t in e["titles"]], "", f"简介：{e['blurb']}", "",
         "话题：" + " ".join(f"#{t.lstrip('#')}" for t in e["tags"]), "", f"置顶评论：{e.get('pinned', '—')}", "",
         "## 合规", "", "- 标注 AI 生成内容；不用\"最强\"\"第一\"等绝对化用语；不用真实人物肖像和真实品牌（crew/14）。",
         f"- 付费卡点集：{'是' if e.get('paywall') else '否'}"]
    open(os.path.join(proj, "交付", "发布包.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    mark(d, "publish")


def st_report(proj, d):
    lines = [f"# 流水线报告（{now()}）", "", "| 阶段 | 状态 |", "|---|---|"]
    for s in STAGES[:-1]:
        lines.append(f"| {s} | {'完成 ' + d['pipeline'][s]['done'] if s in d['pipeline'] else '未完成'} |")
    b = d["budget"]["images"]
    lines += ["", f"出图预算：批准 {b['approved']} 张，已用 {b['used']} 张。", "",
              f"投喂包：{', '.join(d['video_models'])}，在 `交付/投喂包/`。已知不提交的段落：{d.get('known_fails') or '无'}。", "",
              ] + _requirements(proj) + _curve(proj) + _composition(proj) + _colors(proj) + _sound(proj, d) + _cost(proj) + _first(proj) \
        + _coldread(proj) + _packs(proj, d) + [
              "下一步：用户按投喂包出片（或接入出视频接口后由流水线提交），出完把视频交回审片（docs/04 24 分制 + docs/14 4.1 实拍教训）。"]
    open(os.path.join(proj, "logs", "流水线报告.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    mark(d, "report")


def _requirements(proj):
    """用户要求清单（logs/用户要求.md）：用户在对话里说过的硬要求，每次交付前逐条对照。"""
    p = os.path.join(proj, "logs", "用户要求.md")
    if not os.path.exists(p):
        return ["## 用户要求", "", "没有 `logs/用户要求.md`：把用户在对话里说过的硬要求逐条记下（`pipeline.py new` 会建模板），交付前逐条对照。", ""]
    items = [ln.strip() for ln in open(p, encoding="utf-8") if re.match(r"\s*- ", ln)]
    return ["## 用户要求（交付前逐条对照）", ""] + (items or ["（文件里还没有条目）"]) + [""]


def _first(proj):
    p = os.path.join(proj, "交付", "03-首帧图提示词.md")
    if not os.path.exists(p):
        return []
    todo = re.findall(r"^## (\S+)", open(p, encoding="utf-8").read(), re.M)
    done = [c for c in todo if any(os.path.exists(os.path.join(proj, "交付", "首帧图", c + e)) for e in (".png", ".jpg"))]
    return ["## 首帧图（可选）", "", f"{len(todo)} 条值得加首帧图（`交付/03-首帧图提示词.md`），约 ${0.045 * len(todo):.2f}；已定稿 {len(done)} 张"
            + (f"：{'、'.join(done)}" if done else "") + "。出图要用户点头。", ""]


COLD_POINTS = {"故事": ["01-故事.md", "03-剧本.md", "00-立项书.md"],
               "投喂前": ["交付/01-资产提示词.md", "交付/投喂包/seedance", "logs/白模拼图_*.jpg", "交付/参考图定版"],
               "成片": ["交付/成片", "logs/切点对照.jpg", "logs/出片记录.md"]}


def _coldread(proj):
    st = [f"{k}：{'已做' if os.path.exists(os.path.join(proj, 'logs', f'冷读_{k}.md')) else '未做'}" for k in COLD_POINTS]
    return ["## 冷读（不带上下文的一页问题清单，templates/冷读.md）", "", "；".join(st) + "。`pipeline.py coldread <项目> <故事|投喂前|成片>` 生成任务。", ""]


def _composition(proj):
    """构图统计（白模几何检查里量的，不拦；docs/21 第五节）：居中平视的单人镜头全片不超过 2 个。"""
    rows, mid = [], []
    for c in clips(proj):
        p = os.path.join(proj, "logs", f"几何检查_{c}.md")
        if not os.path.exists(p):
            continue
        for ln in open(p, encoding="utf-8"):
            m = re.match(r"\| (\S+) (\S+) \| (\S+) \| (\d+)% \| (\S+) \|", ln)
            if m:
                rows.append(m.group(5))
                if m.group(5) == "居中平视":
                    mid.append(f"{c} 镜{m.group(1)}")
    if not rows:
        return []
    n = len(rows)
    out = ["## 构图统计（白模量的，不拦）", "",
           f"人物镜头 {n} 个：黄金分割附近 {rows.count('黄金分割附近')}、居中 {rows.count('居中') + len(mid)}（其中居中平视 {len(mid)}）、偏边 {rows.count('偏边')}。"]
    if len(mid) > 2:
        out.append(f"居中平视超过 2 个（{'、'.join(mid)}）：换成俯拍、仰拍、过肩、透过前景拍，或让人物偏到 38% / 62%（Eian：全片平视正面最多两次）。")
    return out + [""]


def _colors(proj):
    """色表与黑白对照（tools/color_check.py，只提醒）；Pillow 不可用时跳过。"""
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "color_check.py"), proj], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    warn = [l for l in r.stdout.splitlines() if l.startswith("| ") and "反差太低" in l]
    return ["## 色表与明暗", "", "每张场景图、人物图的主色和黑白反差见 `logs/色表.md`，黑白对照见 `logs/黑白对照.jpg`。"] + \
        ([f"反差太低的图：{len(warn)} 张，转黑白后人和背景分不开，考虑重出或改光。"] if warn else []) + [""]


def _sound(proj, d):
    s = d["pipeline"].get("sound") or {}
    if s.get("skipped") or not s:
        return []
    un = s.get("unheard") or []
    return ["## 声音资产", "", f"{s.get('items', 0)} 段，清单见 `交付/02-声音资产.md`；环境底和配乐用于后期铺底；对白音色由视频模型按音色锁出（有真人录音时才接音色参考）。"] + \
        ([f"**还没人听过**：{'、'.join(x[2:] for x in un)}。请试听，不满意 `review <项目> 声:<文件名> fix <原因>`。"] if un else []) + [""]


def _packs(proj, d):
    """投喂包这次变了哪些条；已经出过片的条变了要提醒（出片记录里填过第几抽的条）。顺手建好出片记录。"""
    rec = os.path.join(proj, "logs", "出片记录.md")
    cs = clips(proj)
    if cs and not os.path.exists(rec):
        rows = [f"| {c.replace('第', '').replace('条', '')} | | {d['video_models'][0]} · 交付/投喂包/{d['video_models'][0]}/{c} | | | | |" for c in cs]
        open(rec, "w", encoding="utf-8").write("\n".join([
            "# 出片记录", "", "> 每条出一次就记一行：第几抽、审片分（docs/04 24 分制）、判决、丢分点（时间点 · 现象 · 怀疑的控制输入）。同一场连续的戏，出完上一条先 `pipeline.py splice` 再 run。", "",
            "| 条 | 第几抽 | 模型 / 投喂包 | 审片分 /24 | 判决 | 丢分点 | 写回框架 |", "|---|---|---|---|---|---|---|"] + rows +
            ["", "一次通过率：— 　平均审片分：—"]) + "\n")
    ch = (d["pipeline"].get("compile") or {}).get("changed") or []
    if not ch:
        return []
    shot = set()
    for ln in open(rec, encoding="utf-8"):
        m = re.match(r"\| ([\d.]+) \| (\S+) \|", ln)
        if m:
            shot.add(f"第{m.group(1)}条")
    warn = sorted({c.split("/", 1)[1].split("_")[0] for c in ch} & shot)
    out = ["## 投喂包这次变了", "", "、".join(ch) + "。"]
    if warn:
        out.append(f"**注意**：{'、'.join(warn)} 已经出过片，投喂包变了：用新包重出前先对比改了什么，旧片段保留。")
    return out + [""]


def _cost(proj):
    """出视频大约花多少钱（config/prices.json，以账单为准；docs/21 第三节）。"""
    p = os.path.join(proj, "分镜.json")
    if not os.path.exists(p):
        return []
    sec = sum(s.get("dur", 0) for c in json.load(open(p, encoding="utf-8")).get("clips", []) for s in c.get("shots", []))
    sys.path.insert(0, TOOLS)
    import genre
    return ["## 出视频估价", "", f"全片 {sec:g} 秒，一次通过、不含重拍：", ""] + [f"- {x}" for x in genre.estimate(sec)] + \
        ["", "单价见 config/prices.json，来源写在每一项；出片后用实际账单校准。", ""]


def _curve(proj):
    """全片能量曲线（分镜.json 的 energy），交付时给用户看节奏。"""
    p = os.path.join(proj, "分镜.json")
    if not os.path.exists(p):
        return []
    clips = json.load(open(p, encoding="utf-8")).get("clips", [])
    rows = ["## 全片曲线", "", "| 条 | 能量 入→出 | 镜头数 | 平均镜长 | 压力源 |", "|---|---|---|---|---|"]
    for c in clips:
        e, sh = c.get("energy") or {}, c.get("shots", [])
        bar = f"{'▮' * e.get('in', 0)} → {'▮' * e.get('out', 0)}"
        rows.append(f"| {c.get('n')} {c.get('title', '')} | {bar} | {len(sh)} | {sum(x.get('dur', 0) for x in sh) / max(1, len(sh)):.1f} 秒 | {c.get('tension', '—')} |")
    return rows + [""]


FUNCS = {s: globals()["st_" + s] for s in STAGES}
for _s in ("assets", "images", "asset_review", "sound"):
    FUNCS[_s] = _season(FUNCS[_s])


# ---------------- 命令 ----------------

def cmd_run(proj, until=None):
    d = load(proj)
    os.makedirs(os.path.join(proj, "logs"), exist_ok=True)
    season = skind(proj) == "season"
    for s in (SEASON_STAGES if season else STAGES):
        try:
            print(f"▶ {s}", flush=True)
            FUNCS[s](proj, d)
            save(proj, d)
        except Stop as e:
            d["pipeline"].pop(s, None)
            for later in STAGES[STAGES.index(s) + 1:]:
                d["pipeline"].pop(later, None)
            d["blocked"] = {"stage": s, "why": str(e), "next": e.next_step, "at": now()}
            save(proj, d)
            print(f"■ 停在 {s}：{e}\nNEXT：{e.next_step}")
            return 2
        if until and s == until:
            d.pop("blocked", None)
            save(proj, d)
            print(f"NEXT：已跑到 {until}，继续请再 run。")
            return 0
    d.pop("blocked", None)
    save(proj, d)
    if season:
        print(f"NEXT：全季资产已就绪。逐集：`pipeline.py episode {os.path.relpath(proj, ROOT)} <集号>` 建集目录，拆 分镜.json，确认剧本后 `run {os.path.relpath(proj, ROOT)}/ep<集号>`")
        return 0
    print("NEXT：全部完成。把 logs/流水线报告.md 的要点和投喂包位置告诉用户；出视频要用户确认花费。")
    return 0


def _golden_files(proj):
    out = ["交付/01-资产提示词.md"] + (["交付/01b-角色压力测试.md"] if os.path.exists(os.path.join(proj, "交付", "01b-角色压力测试.md")) else [])
    out += [os.path.join("交付", "规格单", f) for f in sorted(os.listdir(os.path.join(proj, "交付", "规格单")))]
    for root, _, files in sorted(os.walk(os.path.join(proj, "交付", "投喂包"))):
        out += [os.path.relpath(os.path.join(root, f), proj) for f in sorted(files) if f in ("提示词.txt", "界面参数.txt")]
    return out


def cmd_selftest(example, update=False):
    """把样例拷到临时目录，离线跑完整条流水线，再和 <样例>/标准产物/ 逐字比对。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="aigc_selftest_")
    proj = os.path.join(tmp, os.path.basename(example))
    shutil.copytree(example, proj, ignore=shutil.ignore_patterns("标准产物", "production.json", "交付", "logs", "预演规格"))
    d = load(proj)
    d.update(selftest=True, stress_test=True, project=os.path.basename(example), video_models=["seedance", "hailuo", "wan"])
    d["confirm"] = {"story": "selftest", "script": "selftest"}
    save(proj, d)
    print(f"自测目录：{proj}")
    rc = cmd_run(proj)
    files = []
    if rc == 0 and skind(proj) == "season":  # 剧集样例：全季资产 → 逐集跑完（docs/22）
        import storyboard
        for ep, ed in storyboard.episode_dirs(proj):
            e = load(ed)
            e.update(selftest=True, project=f"{d['project']} 第{ep}集", video_models=d["video_models"])
            e["confirm"] = {"story": "selftest", "script": "selftest"}
            save(ed, e)
            rc = rc or cmd_run(ed)
            if rc == 0:
                files += [os.path.join(f"ep{ep}", f) for f in _golden_files(ed)] + [os.path.join(f"ep{ep}", "交付", "发布包.md")]
        files = ["交付/01-资产提示词.md"] + files
    if rc != 0:
        print("✗ 自测没跑通：流水线在上面那一步停下了。先检查环境（Python 3.9+、Pillow、Blender 4.x、ffmpeg），再看报错。")
        return 1
    gold = os.path.join(example, "标准产物")
    files = files or _golden_files(proj)
    if update:
        shutil.rmtree(gold, ignore_errors=True)
        for f in files:
            os.makedirs(os.path.dirname(os.path.join(gold, f)), exist_ok=True)
            shutil.copy2(os.path.join(proj, f), os.path.join(gold, f))
        print(f"已更新标准产物：{len(files)} 个文件")
        return 0
    bad = []
    want = []
    for root, _, fs in os.walk(gold):
        want += [os.path.relpath(os.path.join(root, f), gold) for f in fs]
    for f in sorted(set(want) | set(files)):
        a, b = os.path.join(proj, f), os.path.join(gold, f)
        if not (os.path.exists(a) and os.path.exists(b)) or open(a, "rb").read() != open(b, "rb").read():
            bad.append(f)
    if bad:
        print(f"✗ 自测不一致：{len(bad)} 个文件和标准产物不同\n  " + "\n  ".join(bad[:20]) +
              f"\n用 diff 对比：{proj}/<文件> 和 {gold}/<文件>。框架代码改了且确认新产物正确时，才用 `selftest <样例> --update` 更新标准。")
        return 1
    print(f"✓ 自测通过：{len(files)} 个产物和标准逐字一致（资产文档、规格单、三个模型的提示词与界面参数）。这台机器、这个智能体可以正式生产。")
    return 0


def cmd_status(proj):
    d = load(proj)
    if skind(proj) == "season":
        import storyboard
        print("全季资产：" + "  ".join(f"{'✓' if s in d['pipeline'] else '·'} {s}" for s in SEASON_STAGES))
        b = d["budget"]["images"]
        print(f"确认：故事 {'✓' if d['confirm']['story'] else '✗'}  设定 {'✓' if d['confirm']['script'] else '✗'}；出图预算 {b['used']}/{b['approved']}")
        for ep, ed in storyboard.episode_dirs(proj):
            if not os.path.isdir(ed):
                print(f"  ep{ep}：未建")
                continue
            e = load(ed)
            done = sum(1 for s in STAGES if s in e["pipeline"])
            st = "完成" if done == len(STAGES) else (f"停在 {e['blocked']['stage']}" if e.get("blocked") else f"{done}/{len(STAGES)}")
            print(f"  ep{ep}：剧本 {'✓' if e['confirm']['script'] else '✗'}，{st}")
        if d.get("blocked"):
            print(f"NEXT：{d['blocked']['next']}")
        return 0
    for s in STAGES:
        print(f"  {'✓' if s in d['pipeline'] else '·'} {s}")
    pend = [k for k, v in d["reviews"].items() if v.get("status") == "待目检"]
    if pend:
        print("待目检：" + "、".join(pend))
    b = d["budget"]["images"]
    print(f"确认：故事 {'✓' if d['confirm']['story'] else '✗'}  剧本 {'✓' if d['confirm']['script'] else '✗'}；出图预算 {b['used']}/{b['approved']}")
    if d.get("blocked"):
        print(f"停在：{d['blocked']['stage']}\n原因：{d['blocked']['why']}\nNEXT：{d['blocked']['next']}")
    elif all(s in d["pipeline"] for s in STAGES):
        print("NEXT：全部完成。")
    else:
        print(f"NEXT：跑 `python3 tools/pipeline.py run {os.path.relpath(proj, ROOT)}`")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "run", "confirm", "approve", "review", "redo", "init", "new", "selftest", "qc", "splice", "episode", "coldread", "model"])
    ap.add_argument("project")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--until", choices=STAGES)
    ap.add_argument("--update", action="store_true", help="selftest：确认新产物正确后更新标准产物")
    a = ap.parse_args()
    if a.cmd == "selftest" and a.project == "all":  # 两套样例都跑：单片 + 剧集
        rc = max(cmd_selftest(os.path.join(ROOT, "examples", e), a.update) for e in ("样例-便利店", "样例-剧集"))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "post.py"), "--selftest"], capture_output=True, text=True)
        print((r.stdout.strip().splitlines() or ["✗ post.py 自测没有输出：" + r.stderr[-400:]])[-1])
        return max(rc or 0, r.returncode)
    proj = os.path.abspath(a.project)
    if a.cmd == "coldread":  # 冷读（templates/冷读.md）：给一个不带上下文的子代理的任务书
        point = a.args[0] if a.args else ""
        if point not in COLD_POINTS:
            sys.exit(f"冷读点只有：{' / '.join(COLD_POINTS)}")
        import glob
        mats = [m for pat in COLD_POINTS[point] for m in sorted(glob.glob(os.path.join(proj, pat)))]
        req = os.path.join(proj, "logs", "用户要求.md")
        tpl = open(os.path.join(ROOT, "templates", "冷读.md"), encoding="utf-8").read()
        brief = tpl.split("<!-- 任务书 -->", 1)[1].strip().replace("<冷读点>", point).replace("<项目>", proj).replace(
            "<材料>", "\n".join(f"- {m}" for m in mats) or "- （这个点的材料还没有）").replace(
            "<要求>", req if os.path.exists(req) else "（没有用户要求清单）").replace("<输出>", os.path.join(proj, "logs", f"冷读_{point}.md"))
        out = os.path.join(proj, "logs", f"冷读_{point}_任务.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w", encoding="utf-8").write(brief + "\n")
        print(brief + f"\n\n（任务书已写到 {os.path.relpath(out, ROOT)}。派一个全新的子代理，只把这份任务书给它；它写完后在报告里看结论。）")
        return 0
    if a.cmd == "model":  # 切换出片模型（4.0 alpha.10）：改 video_models，compile 阶段下次 run 重编
        want = [m.strip() for x in a.args for m in x.split(",") if m.strip()]
        bad = [m for m in want if m not in MODEL_ROUTE]
        if not want or bad:
            sys.exit("模型只能是：" + " / ".join(MODEL_ROUTE) + "，可以用逗号连写，如 wan,hailuo")
        d = load(proj)
        d["video_models"] = want
        d["pipeline"].pop("compile", None)
        save(proj, d)
        for m in want:
            print(f"■ {m}：{MODEL_ROUTE[m]}")
        print("已切换。下一步：python3 tools/pipeline.py run " + os.path.relpath(proj, ROOT) + "（只重编投喂包，不重出图和白模）")
        return 0
    if a.cmd == "new":  # 单片 JSON 模式建项目（docs/18）：production.json + 立项书模板 + logs
        title = a.args[0] if a.args else os.path.basename(proj)
        os.makedirs(os.path.join(proj, "logs"), exist_ok=True)
        d = load(proj)
        d.update(project=title, video_models=d.get("video_models") or ["seedance", "hailuo", "wan"])
        save(proj, d)
        lx = os.path.join(proj, "00-立项书.md")
        if not os.path.exists(lx):
            open(lx, "w", encoding="utf-8").write(open(os.path.join(ROOT, "templates", "00-立项书.md"), encoding="utf-8").read().replace("<项目名>", title))
        ur = os.path.join(proj, "logs", "用户要求.md")
        if not os.path.exists(ur):
            open(ur, "w", encoding="utf-8").write("# 用户要求\n\n> 用户在对话里说过的硬要求，一条一行，写清哪天说的；每次停下来、交付前逐条对照（流水线报告会列出来）。\n\n")
        fq = os.path.join(proj, "logs", "框架问题.md")
        if not os.path.exists(fq):
            open(fq, "w", encoding="utf-8").write("# 框架问题\n\n| # | 日期 | 阶段 | 问题 | 建议 | 状态 |\n|---|---|---|---|---|---|\n")
        print(f"已建 {os.path.relpath(proj, ROOT)}（production.json、00-立项书.md、logs/）。NEXT：按 docs/09 第零节的入口走：故事确认后 `confirm {os.path.relpath(proj, ROOT)} story`")
        return 0
    if a.cmd == "init":
        title, short = a.args
        os.makedirs(os.path.join(proj, "预演规格"), exist_ok=True)
        for f in ("build_assets.py", "gen_clips.py"):
            dst = os.path.join(proj, "预演规格", f)
            if not os.path.exists(dst):
                s = open(os.path.join(ROOT, "templates", "project", f), encoding="utf-8").read()
                open(dst, "w", encoding="utf-8").write(s.replace("{{片名}}", title).replace("{{短名}}", short))
        d = load(proj)
        d["project"] = title
        save(proj, d)
        print(f"已建好 {proj}（两个脚本 + production.json）。NEXT：用户确认故事和剧本后，把剧本写进两个脚本，再 run")
        return 0
    if not os.path.isdir(proj):
        sys.exit(f"没有这个项目目录：{proj}")
    if a.cmd == "episode":  # 剧集模式：建一集的目录（docs/22）
        if skind(proj) != "season":
            sys.exit("episode 要在剧目录（有 季.json）上用")
        ep = a.args[0].replace("ep", "").zfill(2)
        ed = os.path.join(proj, f"ep{ep}")
        os.makedirs(os.path.join(ed, "logs"), exist_ok=True)
        sd, e = load(proj), load(ed)
        e.update(project=f"{sd.get('project', os.path.basename(proj))} 第{ep}集", video_models=sd.get("video_models", ["seedance"]))
        e["confirm"]["story"] = "全季"
        if sd.get("selftest"):
            e["selftest"] = True
        save(ed, e)
        print(f"已建 {os.path.relpath(ed, ROOT)}。NEXT：把第 {ep} 集剧本拆成 {os.path.relpath(ed, ROOT)}/分镜.json（设定用剧目录的），用户确认剧本后 `confirm {os.path.relpath(ed, ROOT)} script`，再 run")
        return 0
    if a.cmd == "selftest":
        return cmd_selftest(proj, a.update)
    if a.cmd == "qc":
        import qc_video
        return qc_video.main(proj, a.args[0], a.args[1])
    if a.cmd == "splice":
        n = a.args[0].replace("第", "").replace("条", "")
        out = os.path.join(proj, "交付", "衔接", f"第{n}条_尾帧.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.1", "-i", a.args[1], "-frames:v", "1", "-update", "1", out],
                           capture_output=True, text=True)
        if r.returncode or not os.path.exists(out):
            sys.exit(f"截尾帧失败：{r.stderr.strip()[:300]}")
        print(f"已截第{n}条尾帧：{os.path.relpath(out, proj)}。NEXT：跑 `python3 tools/pipeline.py run {os.path.relpath(proj, ROOT)}` 重编下一条的投喂包")
        return 0
    if a.cmd == "status":
        return cmd_status(proj)
    if a.cmd == "run":
        return cmd_run(proj, a.until)
    d = load(proj)
    if a.cmd == "confirm":
        d["confirm"][a.args[0]] = now()
    elif a.cmd == "approve":
        kind, n = a.args
        b = d["budget"].setdefault(kind, {"approved": 0, "used": 0})  # images：张数；video：元（tools/produce.py）
        b["approved"] = b.get("used", 0) + int(n)
    elif a.cmd == "review":
        key, verdict = a.args[0], a.args[1]
        if key not in d["reviews"]:
            sys.exit(f"没有这个待目检条目：{key}；用 status 看")
        d["reviews"][key].update(status=verdict, note=" ".join(a.args[2:]), at=now())
    elif a.cmd == "redo":
        n = int(a.args[0])
        it = asset_table(proj)[n]
        src = os.path.join(proj, "交付", "资产图", it["file"])
        if os.path.exists(src):
            dst = os.path.join(proj, "交付", "资产图", "废片")
            os.makedirs(dst, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%m%d%H%M")
            for ext in (".png", ".json"):
                p = os.path.splitext(src)[0] + ext
                if os.path.exists(p):
                    shutil.move(p, os.path.join(dst, os.path.basename(os.path.splitext(src)[0]) + f"_{stamp}{ext}"))
        d["reviews"].pop(_img_key(it), None)
    save(proj, d)
    print("已记录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
