#!/usr/bin/env python3
"""影调试拍（4.0 alpha.14，《好日子》质感复盘）：批量出资产之前，用 2–3 套候选影调各拍几张关键时刻，用户挑一套写进 film_look。

用法：
  python3 -I tools/look_test.py <项目>            写 交付/影调试拍.md（出图清单格式，tools/image_gen.py 能直接读）
  python3 -I tools/image_gen.py <项目>/交付/影调试拍.md --out <项目>/交付/影调试拍 --dry-run   先看会出哪些图
  python3 -I tools/image_gen.py <项目>/交付/影调试拍.md --out <项目>/交付/影调试拍              出图（计费）
  python3 -I tools/look_test.py <项目> --sheet    把出好的图拼成 交付/影调试拍/对比.jpg（每行一套影调，每列一个时刻）

设定.json：
  "look_test": {
    "options": [{"id": "A", "name": "宽银幕白昼", "zh": "中文一行", "en": "English one line", "camera": "C05"（可选，摄影机预设）, "grain_in_stills": false}, …],
    "moments": [{"loc": "场景名", "en": "这一刻画面里发生什么（英文，一两句，含人物的大概样子和机位）"}, …]
  }
挑定之后把选中的那一套（去掉 id、name）写成 "film_look"，之后所有场景图和视频提示词逐字带上。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    proj = sys.argv[1]
    bible = json.load(open(os.path.join(proj, "设定.json"), encoding="utf-8"))
    lt = bible.get("look_test") or sys.exit("设定.json 没有 look_test（options + moments），见本文件说明")
    locs = {l["name"]: l for l in bible["locations"]}
    rec = {r["id"]: r for r in json.load(open(os.path.join(ROOT, "config", "style_recipes.json"), encoding="utf-8"))["配方"]}
    cams = {c["id"]: c for c in json.load(open(os.path.join(ROOT, "config", "camera_presets.json"), encoding="utf-8"))["预设"]}
    out_dir = os.path.join(proj, "交付", "影调试拍")
    if "--sheet" in sys.argv:
        return sheet(lt, out_dir)
    rows, blocks, n = [], [], 0
    for op in lt["options"]:
        for j, m in enumerate(lt["moments"], 1):
            n += 1
            l = locs[m["loc"]]
            fname = f"影调{op['id']}_时刻{j}.png"
            rows.append(f"| {n} | @look_{op['id']}_{j} | {fname} | 影调试拍 | Image 2.5（APIMart 2K） | — |")
            prompt = (f"A single frame from a feature film, 16:9, full frame with no black bars, no text. {m['en']}\n\n"
                      f"Location: {l['en']} {l.get('must_en', '')}\n\n"
                      + (f"Film look: {op['en']}" if op.get("en") else "")
                      + (f"\n\nCamera: shot on {cams[op['camera']]['camera']} with {cams[op['camera']]['lens']} lenses"
                         + (f" on {cams[op['camera']]['stock']}" if cams[op['camera']].get("stock") else "")
                         + f"; {cams[op['camera']]['effects_en' if op.get('grain_in_stills') else 'stills_en']}" if op.get("camera") else ""))  # 试拍只比影调：不带世界配方（R21 的"平光、无阴影"会和暗调方案打架）
            blocks.append(f"### {n}｜@look_{op['id']}_{j}｜影调 {op['id']}「{op['name']}」× 时刻 {j}（{m['loc']}）\n\n```text\n{prompt}\n```\n")
    doc = ["# 影调试拍", "", f"> {len(lt['options'])} 套影调 × {len(lt['moments'])} 个时刻 = {n} 张。挑一套写进 设定.json 的 film_look。", "",
           "## 候选影调", ""] + [f"- **{o['id']} {o['name']}**：{o['zh']}" for o in lt["options"]] + [
           "", "## 出图清单", "", "| 序 | 标签 | 文件名 | 类型 | 推荐模型 | 先要 |", "|---|---|---|---|---|---|"] + rows + ["", "## 提示词", ""] + blocks
    path = os.path.join(proj, "交付", "影调试拍.md")
    open(path, "w", encoding="utf-8").write("\n".join(doc))
    print(f"写好 {path}：{n} 张。先 --dry-run 看，再出图（计费）。")


def sheet(lt, out_dir):
    files = [[os.path.join(out_dir, f"影调{o['id']}_时刻{j}.png") for j in range(1, len(lt["moments"]) + 1)] for o in lt["options"]]
    have = [[f for f in r if os.path.exists(f)] for r in files]
    if not all(len(r) == len(files[0]) for r in have):
        sys.exit("还有图没出完，先出完再拼")
    rows = []
    for i, r in enumerate(have):
        args = sum((["-i", f] for f in r), [])
        out = os.path.join(out_dir, f"_row{i}.jpg")
        k = len(r)
        fc = "".join(f"[{j}]scale=640:-2[s{j}];" for j in range(k)) + "".join(f"[s{j}]" for j in range(k)) + f"hstack={k}"
        subprocess.run(["ffmpeg", "-v", "error", "-y"] + args + ["-filter_complex", fc, out], check=True)
        rows.append(out)
    out = os.path.join(out_dir, "对比.jpg")
    args = sum((["-i", f] for f in rows), [])
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + args + ["-filter_complex", "".join(f"[{i}]" for i in range(len(rows))) + f"vstack={len(rows)}", "-q:v", "3", out], check=True)
    for f in rows:
        os.remove(f)
    print(f"对比图：{out}（每行一套影调：{'、'.join(o['id'] + ' ' + o['name'] for o in lt['options'])}）")


if __name__ == "__main__":
    main()
