# 白模预演工具（投喂包里 @视频1 的结构来源）

一条生成写一份镜头规格 JSON，一条命令渲染出 `previs_<名>.mp4`（作 Seedance 的 `@视频1`）和每镜中点的关键帧拼图。

```bash
python3 tools/previs/render_previs.py tools/previs/examples/示例_门口对峙.json --out 交付/预演/ --stills-only   # 先看每镜构图
python3 tools/previs/render_previs.py tools/previs/examples/示例_门口对峙.json --out 交付/预演/                 # 再渲全片
```

需要 Blender 4.x 和 ffmpeg。Blender 路径用环境变量 `BLENDER_BIN` 指定；不设时依次找 macOS 默认路径和 PATH 里的 `blender`。12 秒、3 镜的示例在 M 系列 Mac 上约 17 秒渲完。

## 规格字段

坐标是米制，Z 轴向上。时间单位是秒。

```jsonc
{
  "name": "第2条",                 // 输出文件名：previs_第2条.mp4
  "duration": 30, "fps": 24, "resolution": [1280, 720],
  "world_color": [0.55, 0.58, 0.62],
  "boxes": [                        // 场景：每个都是长方体，用包围盒定义
    {"name": "Road", "min": [-2, 0, -2], "max": [2, 400, 0], "color": [0.36, 0.36, 0.36]},
    {"name": "Truck", "min": [-0.95, 117.5, 0], "max": [0.95, 122.5, 1.8], "color": [0.85, 0.85, 0.82],
     "keys": [{"t": 14, "offset": [0, 0, 0]}, {"t": 15.5, "offset": [0, 0, -2.5]}]}   // 可动：相对初始位置的位移，可加 "scale": [x,y,z]
  ],
  "people": [                       // 人物：圆柱 + 球头代理，每人一个识别色，胸前的深色块表示正面
    {"name": "岚", "color": [0.85, 0.25, 0.10], "height": 1.65,
     "keys": [{"t": 0, "pos": [0, 100, 0], "face": 180, "lean": 15},   // face：绕 Z 的角度，0 = 正面朝 -Y，180 = 朝 +Y；lean：前倾角
              {"t": 18, "pos": [0, 0.4, 0], "face": 180}]}
  ],
  // 巨物："shape": "giant" 时不用小人，换成树干身子 + 外撇根须 + 四根斜枝 + 空心环头 + 几圈浅色布带（名字仍是 <名>_body / <名>_head）；
  //       keys 里写 "head": [x,y,z] 的时刻，空心头落到那个点，脖子从树干顶拉到环顶（俯身）
  // 坐车：people 里 "ride": "prop_红车", "seat": [x, y, z]，人和注视点用约束挂在车上；props 里 "vehicle": true 按车速查
  // 挂载机位：shots 里 "mount": 人名（挂在头上）或 "prop_红车"，"mount_off": [x,y,z]，"looks": [{"t": 秒, "yaw": 度, "pitch": 度}]（转头），
  //       "hide": [这一镜藏起来的人或道具]，"hood": {"size", "off", "yaw", "color"}（车内引擎盖），"bob": 走跑上下颠的幅度（米）
  //       "see": ["prop_红车"]（规格单里：画面里要拍到的道具，几何检查按道具查）
  "light": {"preset": "sunset", "from": [1, -1]},   // 可选：光型 noon / dawn / sunset / dusk / overcast / night，可覆盖 elev、energy、sun、sky、sky_strength；
                                                    //       写了就换 EEVEE + 太阳灯（真影子，物体照旧用识别色），不写保持 Workbench 平光
  "shots": [                        // 镜头：从 start 开始，到下一个镜头的 start 硬切
    {"start": 0, "lens": 85, "keys": [{"t": 0, "pos": [0, -14, 9], "rot": [83, 0, 0]}]},       // 固定机位：rot = [俯仰(90 为平视), 0, 水平朝向]
    {"start": 4, "lens": 50, "look_at": "岚", "handheld": 0.04,                                 // look_at：人名（盯胸口）或 [x,y,z]；handheld：手持晃动幅度（米）
     "keys": [{"t": 4, "pos": [-4, 91, 1.1]}, {"t": 9, "pos": [-4, 66, 1.1]}],                // 两个以上关键帧 = 运镜，之间线性插值
     "ease": true}                                                                               // 可选：缓入缓出（两端平的贝塞尔）；跟拍移动的人或道具时不要加，构图会漂
  ]
}
```

## 换算与约定

- **焦段 → 视场角**（36mm 全画幅横向）：24mm ≈ 74°，35mm ≈ 54°，50mm ≈ 40°，85mm ≈ 24°，135mm ≈ 15°。提示词里写的视场角要和这里的焦段对上。
- **机位朝向**：`rot` 的第一个数是俯仰，90 = 水平，小于 90 是俯拍；第三个数是水平朝向，0 = 看向 +Y，90 = 看向 -X，-90 = 看向 +X。
- **人物落点**：人物走向镜头时，停在离镜头至少 2 米处（《圣经》第 21 条）。
- **切点就是 shots 的 start**：要和提示词里的切点清单逐个对上（第 25、28 条）。
- 识别色写进 A 版素材职责："识别色对应：橙红色圆柱 = 岚，蓝色圆柱 = 海生"。

## 什么时候手写脚本

地形复杂（斜坡、台阶）、水位上涨、扫过的光带这类规格 JSON 表达不了的东西，直接用 `common.py` 手写 Blender 脚本。`examples/潮水来之前_第2条.py`（七镜、斜坡石阶、浪、光带）和 `examples/潮水来之前_第6b条.py`（缓入缓出后拉升高）是两份完整范例：

```bash
"$BLENDER_BIN" -b --factory-startup -P tools/previs/examples/潮水来之前_第2条.py -- /tmp/g2_frames
ffmpeg -framerate 24 -i /tmp/g2_frames/f_%04d.png -c:v libx264 -pix_fmt yuv420p previs_第2条.mp4
```

切点检查：`ffmpeg -i previs.mp4 -vf "select='gt(scene,0.08)',metadata=print" -f null -` 列出画面突变的时间点，应与切点清单一致。

## 零抽卡闸门 · 几何检查

`python3 tools/previs/geometry_check.py <白模规格.json> <镜头规格单.json> [--out 报告.md]`：在 Blender 里搭出白模（`build_from_spec.py --no-render`），逐镜检查主体入画、大小、遮挡、头后竖直物体、机位穿墙、越轴。镜头规格单的格式见 docs/14。
