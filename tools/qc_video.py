#!/usr/bin/env python3
"""审片机检：出片后一条命令，把能量化的都量出来，写成 logs/审片_第N条.md，智能体在此基础上看画面打 24 分（docs/04）。

  python3 tools/pipeline.py qc <项目目录> <第N条> <视频文件>

量什么：
  时长     和规格单是否一致（±0.3 秒）
  切点     ffmpeg 场景切换检测，对比规格单每镜 t0（±0.3 秒算对上）
  声音     每镜平均音量和每 0.5 秒滑动 1 秒窗口的最低音量；规格单写了声音或台词的镜头，有一段 < -35 dB 就标出来
  台词     装了 faster-whisper 就听写，逐句和规格单台词比对（相似度）；没装就跳过并说明
  拼图     每 0.5 秒一帧，logs/审片拼图_第N条.jpg，给智能体目检
  逐镜     每镜每秒 4 帧一张图，logs/审片逐镜_第N条/镜K.jpg：看站位、朝向、前后景、道具在谁手里（4.0 alpha.12）
  并排     有白模的条，白模和成片左右并排、每 0.5 秒一帧，logs/审片并排_第N条.jpg：
           机位、构图、切点对不对得上，每个颜色变成了谁，有没有白模形状残留（学自 scenario-labs blockout skill）
"""
import difflib
import json
import math
import os
import re
import subprocess
import sys

TH_SILENT = -35.0  # 第 1 条实拍：长廊那段 -36～-43 dB，听起来就是没声


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def duration(video):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video])
    return float(r.stdout.strip())


def cuts(video):
    r = sh(["ffmpeg", "-i", video, "-vf", "select='gt(scene,0.25)',showinfo", "-f", "null", "-"])
    return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]


def rms_db(video, t0, t1):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t0), "-t", str(max(0.1, t1 - t0)), "-i", video, "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True)
    import array
    a = array.array("h", p.stdout)
    if not a:
        return -99.0
    return 20 * math.log10(math.sqrt(sum(x * x for x in a) / len(a)) / 32768 + 1e-9)


def transcribe(video):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        try:  # 没装 faster-whisper 时用 openai-whisper（本机已装 medium 模型）
            import whisper
        except ImportError:
            return None
        m = whisper.load_model(os.environ.get("QC_WHISPER_OPENAI", "medium"))
        r = m.transcribe(video, language="zh", word_timestamps=True, condition_on_previous_text=False)
        return [(w["start"], w["end"], w["word"]) for s in r["segments"] for w in s.get("words", [])]
    m = WhisperModel(os.environ.get("QC_WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2"), device="cpu", compute_type="int8")
    segs, _ = m.transcribe(video, language="zh", word_timestamps=True, initial_prompt="以下是普通话句子。")
    return [(w.start, w.end, w.word) for s in segs for w in s.words]  # 逐字时间，按镜头切


def norm(t):
    return re.sub(r"[，。！？、…\s,.!?—]", "", t)


def main(proj, clip, video):
    spec_p = os.path.join(proj, "交付", "规格单", f"{clip}.json")
    if not os.path.exists(spec_p):
        print(f"没有规格单 {spec_p}")
        return 1
    spec = json.load(open(spec_p, encoding="utf-8"))
    video = os.path.abspath(video)
    os.makedirs(os.path.join(proj, "logs"), exist_ok=True)  # 新项目还没有 logs 时，拼图会悄悄写不出来
    out = [f"# 审片机检：{clip}（{os.path.basename(video)}）", ""]
    D = duration(video)
    ok_d = abs(D - spec["duration"]) <= 0.3
    out.append(f"- 时长：{D:.2f} 秒，规格 {spec['duration']} 秒 → {'对' if ok_d else '不对'}")
    cs = cuts(video)
    want = [s["t0"] for s in spec["shots"]][1:]
    hit = [w for w in want if any(abs(c - w) <= 0.3 for c in cs)]
    extra = [c for c in cs if all(abs(c - w) > 0.3 for w in want)]
    out.append(f"- 切点：规格 {want}，实测 {[round(c, 2) for c in cs]} → 对上 {len(hit)}/{len(want)}" + (f"；多出 {[round(c, 2) for c in extra]}（模型自己加了镜头）" if extra else ""))
    out += ["", "| 镜 | 时间 | 音量（每秒最低） | 规格里的声音 / 台词 | 提示 |", "|---|---|---|---|---|"]
    for s in spec["shots"]:
        db = rms_db(video, s["t0"], s["t1"])
        starts = [s["t0"] + 0.15 + 0.5 * i for i in range(int((s["t1"] - s["t0"] - 0.3) / 0.5) + 1)]
        wins = [rms_db(video, x, min(x + 1.0, s["t1"] - 0.15)) for x in starts if s["t1"] - 0.15 - x >= 0.5]
        low = min(wins) if wins else db
        want_sound = re.findall(r"<([^>]+)>", s["body"]) + re.findall(r"\{([^}]+)\}", s["body"])
        flag = "有一段几乎没声：模型没出这段声音，先查画里有没有声源，缺了再后期补" if want_sound and low < TH_SILENT else ""
        out.append(f"| {s['id']} {s['name']} | {s['t0']}–{s['t1']} | 平均 {db:.0f} / 最低 {low:.0f} dB | {'；'.join(want_sound) or '—'} | {flag} |")
    lines = [(s["t0"], s["t1"], x) for s in spec["shots"] for x in re.findall(r"\{([^}]+)\}", s["body"])]
    asr = transcribe(video) if lines else []
    out.append("")
    if asr is None:
        out.append("- 台词：没装 faster-whisper，未听写（`pip install faster-whisper` 后重跑）；请人工听。")
    elif lines:
        out += ["| 规格台词 | 时间 | 听写 | 相似度 |", "|---|---|---|---|"]
        for t0, t1, txt in lines:
            heard = "".join(w[2] for w in asr if t0 - 0.3 <= w[0] < t1 + 0.1)
            r = difflib.SequenceMatcher(None, norm(txt), norm(heard)).ratio()
            out.append(f"| {txt} | {t0}–{t1} | {heard or '（没听到）'} | {r:.0%}{' ⚠' if r < 0.6 else ''} |")
        out.append("\n听写会把同音字听错（如\"劈中\"听成\"批中\"），相似度 ≥ 60% 一般就是说对了；低于的要人工听。")
    sheet = os.path.join(proj, "logs", f"审片拼图_{clip}.jpg")
    sh(["ffmpeg", "-v", "error", "-y", "-ss", "0.25", "-i", video, "-vf", "fps=2,scale=320:-1,tile=10x6", "-frames:v", "1", sheet])
    per = os.path.join(proj, "logs", f"审片逐镜_{clip}")
    os.makedirs(per, exist_ok=True)
    for s in spec["shots"]:  # 逐镜每秒 4 帧（审片标准：站位、朝向、前后景、道具在谁手里、手的身份都要逐镜看）
        n = max(1, int(math.ceil((s["t1"] - s["t0"]) * 4)))
        sh(["ffmpeg", "-v", "error", "-y", "-ss", str(s["t0"]), "-t", str(s["t1"] - s["t0"]), "-i", video,
            "-vf", f"fps=4,scale=480:-1,tile=4x{int(math.ceil(n / 4))}", "-frames:v", "1", os.path.join(per, f"镜{s['id']}.jpg")])
    previs = os.path.join(proj, "交付", spec.get("previs", "")) if spec.get("previs") else ""
    side = ""
    if previs and os.path.exists(previs):  # 白模和成片并排：机位、切点、每个颜色变成了谁、有没有白模形状残留
        side = os.path.join(proj, "logs", f"审片并排_{clip}.jpg")
        sh(["ffmpeg", "-v", "error", "-y", "-i", previs, "-i", video, "-filter_complex",
            "[0:v]fps=2,scale=-2:270[a];[1:v]fps=2,scale=-2:270[b];[a][b]hstack,tile=3x10", "-frames:v", "1", side])
    out += ["", f"- 拼图：`logs/审片拼图_{clip}.jpg`（每 0.5 秒一帧）",
            f"- 逐镜：`logs/审片逐镜_{clip}/镜K.jpg`（每镜每秒 4 帧）"]
    if side:
        out.append(f"- 白模并排：`logs/审片并排_{clip}.jpg`（左白模、右成片，每 0.5 秒一对）")
    out += ["", "## 逐镜目检（先填这张，再打分；docs/14 4.3）", "",
            "每镜看：谁坐谁站、面朝谁、在画面哪一侧；姿势、发型和上一镜一致吗；前景背景有没有多出来的人或东西；"
            "道具在谁手里、谁递给谁；手接得上对的肩膀吗；要看清的脸看得见吗；台词音量和情绪对吗；表演和肢体有没有夸张、多余的动作。", "",
            "| 镜 | 写的 | 实际 | 判断 |", "|---|---|---|---|"]
    out += [f"| {s['id']} {s['name']} |  |  |  |" for s in spec["shots"]]
    out += ["",
            "## 智能体目检（按 docs/04 24 分制，对照每镜「画面里」和 docs/14 4.1）", "",
            "| 维度 | 权重 | 得分 0/1/2 | 依据（时间点 + 现象） |", "|---|---|---|---|"]
    for dim, w in (("D1 叙事完成", "×2"), ("D2 身份锁", "×2"), ("D3 表演", "×2"), ("D4 空间连续", "×1.5"), ("D5 运动与物理", "×1.5"),
                   ("D6 摄影", "×1"), ("D7 台词与声音", "×1"), ("D8 画面卫生", "×1")):
        out.append(f"| {dim} | {w} |  |  |")
    os.makedirs(os.path.join(proj, "logs"), exist_ok=True)
    rep = os.path.join(proj, "logs", f"审片_{clip}.md")
    open(rep, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print("\n".join(out[:4]))
    print(f"报告：{rep}\nNEXT：打开拼图和视频，填完报告里的 24 分表；< 20 分写修法（只改一行），≥ 20 分入选。")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
