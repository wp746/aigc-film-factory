#!/usr/bin/env python3
"""用 API 提交一个投喂包（compile_video.py 的产物）去出片：Seedance 2.5（火山方舟）、海螺 H3（MiniMax）、万相 3.0（阿里云百炼）。

用法：
  python3 -I tools/video_submit.py <投喂包目录>                 # 只打印会提交什么、预计多少钱，不计费
  python3 -I tools/video_submit.py <投喂包目录> --confirm       # 真正提交、轮询、把成片下载到投喂包目录
  可选 --url-map urls.json：本地文件 → 公网 URL 的对照（参考视频在方舟和百炼只能用公网 URL）

密钥只从环境变量或 .env 读：ARK_API_KEY；MINIMAX_API_KEY；DASHSCOPE_API_KEY + WAN_BASE_URL（形如
https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com）。
付费：每次提交都计费，工厂规则要求用户确认费用后才加 --confirm。
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICE_NOTE = {"seedance": "以火山方舟控制台计费为准（Seedance 2.5 按秒和分辨率计费）",
              "hailuo": "以 MiniMax 控制台计费为准（H3 只有按量付费）",
              "wan": "约 ¥0.30/秒（480P）、¥0.60/秒（720P）、¥1.20/秒（1080P），带参考视频时白模秒数也计费；第三方转述，以百炼账单为准"}


def load_env():
    for d in (os.getcwd(), ROOT):
        p = os.path.join(d, ".env")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith("#"):
                    os.environ.setdefault(m.group(1), m.group(2).strip("'\""))


def data_uri(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(open(path, "rb").read()).decode()


def resolve(url, kind, model, url_map):
    """把 file:// 占位换成可用的地址：能用 Base64 的直接内嵌，不能的查 --url-map。"""
    if not url.startswith("file://"):
        return url
    path = url[len("file://"):]
    if path in url_map:
        return url_map[path]
    base64_ok = {"seedance": ("image", "audio"), "hailuo": ("image", "video", "audio"), "wan": ("image",)}[model]
    if kind in base64_ok:
        return data_uri(path)
    sys.exit(f"{os.path.basename(path)}：{model} 的参考{ {'video': '视频', 'audio': '音频'}.get(kind, kind) }只能用公网 URL。"
             f"先上传到对象存储（设为公共读），再用 --url-map 传入 {{\"{path}\": \"https://…\"}}")


def http(method, url, headers, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers=dict(headers, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def model_of(pack):
    return os.path.basename(os.path.dirname(os.path.abspath(pack)))


def prepare(req, model, url_map):
    req = json.loads(json.dumps(req))
    if model == "seedance":
        for c in req["content"]:
            for k in ("image_url", "video_url", "audio_url"):
                if k in c:
                    c[k]["url"] = resolve(c[k]["url"], k.split("_")[0], model, url_map)
    elif model == "hailuo":
        for c in req["content"]:
            if "url" in c:
                c["url"]["url"] = resolve(c["url"]["url"], c["type"].split("_")[0], model, url_map)
    elif model == "wan":
        for m in req["input"]["media"]:
            m["url"] = resolve(m["url"], m["type"].split("_")[-1] if m["type"] != "reference_image" else "image", model, url_map)
    return {k: v for k, v in req.items() if not k.startswith("_")}


def submit(model, body):
    if model == "seedance":
        h = {"Authorization": "Bearer " + os.environ["ARK_API_KEY"]}
        tid = http("POST", "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks", h, body)["id"]
        q = lambda: http("GET", f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{tid}", h)
        done = lambda r: (r.get("status") in ("succeeded", "failed", "expired"), r.get("content", {}).get("video_url"))
    elif model == "hailuo":
        h = {"Authorization": "Bearer " + os.environ["MINIMAX_API_KEY"]}
        tid = http("POST", "https://api.minimax.io/v2/video_generation", h, body)["task_id"]
        q = lambda: http("GET", f"https://api.minimax.io/v2/video_generation/{tid}", h)
        done = lambda r: (r.get("status") in ("succeeded", "failed", "cancelled"), (r.get("video") or {}).get("url") or r.get("video_url"))
    else:
        base = os.environ["WAN_BASE_URL"].rstrip("/")
        h = {"Authorization": "Bearer " + os.environ["DASHSCOPE_API_KEY"], "X-DashScope-Async": "enable"}
        tid = http("POST", base + "/api/v1/services/aigc/video-generation/video-synthesis", h, body)["output"]["task_id"]
        q = lambda: http("GET", f"{base}/api/v1/tasks/{tid}", {"Authorization": h["Authorization"]})
        done = lambda r: (r["output"]["task_status"] in ("SUCCEEDED", "FAILED", "CANCELED"), r["output"].get("video_url"))
    print(f"已提交，任务 {tid}，开始轮询…")
    for _ in range(240):
        time.sleep(15)
        r = q()
        fin, url = done(r)
        if fin:
            return tid, url, r
    sys.exit(f"任务 {tid} 一小时没完成，稍后用控制台查看")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--url-map", default=None)
    a = ap.parse_args()
    load_env()
    model = model_of(a.pack)
    if model not in PRICE_NOTE:
        sys.exit(f"不支持的模型：{model}（只支持 Seedance 2.5 / 海螺 H3 / 万相 3.0）")
    req = json.load(open(os.path.join(a.pack, "api_request.json"), encoding="utf-8"))
    dur = req.get("duration") or req.get("parameters", {}).get("duration")
    print(f"模型：{model}　时长：{dur} 秒　费用：{PRICE_NOTE[model]}")
    print(open(os.path.join(a.pack, "检查.txt"), encoding="utf-8").read())
    if not a.confirm:
        print("未提交（没有 --confirm）。确认费用后再加 --confirm。")
        return
    url_map = json.load(open(a.url_map, encoding="utf-8")) if a.url_map else {}
    body = prepare(req, model, url_map)
    tid, url, r = submit(model, body)
    json.dump(r, open(os.path.join(a.pack, f"结果_{tid}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not url:
        sys.exit(f"任务结束但没有成片：{json.dumps(r, ensure_ascii=False)[:500]}")
    dst = os.path.join(a.pack, f"成片_{tid}.mp4")
    urllib.request.urlretrieve(url, dst)
    print("成片：" + dst)


if __name__ == "__main__":
    main()
