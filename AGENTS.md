# AGENTS.md

本仓库是 **AIGC 制片工厂**：把一句创意或剧本做成可直接投喂 Seedance 2.5 / 海螺 H3 / 万相 3.0 的视频提示词包和资产出图提示词。

**任何智能体（Codex、WorkBuddy / CodeBuddy、Cursor、Claude Code 等）在本仓库工作时，先完整读 `SKILL.md`，按它的流程与"按需加载"表执行。** 本文件只是入口。

## 最高标准：不做电子垃圾

故事、剧本、提示词、成片都按 `docs/20-顶级标准.md`：主角自己做选择并付出代价，不做认亲、系统、战神这类等救主结构；每条有价值翻转和视听高光；每个岗位做到顶级影视团队的水平。立项时先让用户挑类型卡和时长档位（`docs/21`，`python3 -I tools/genre.py --menu`）。每个新项目开工前先联网研究 3 部同类型顶级作品，每条审完把教训写成规则（docs/20 第七节）。

## 剧本确认之后：只走流水线（最重要）

```
python3 tools/pipeline.py selftest examples/样例-便利店   # 第一次用先自测：不花钱，产物要和标准逐字一致
python3 tools/pipeline.py status projects/<项目>    # 看最后一行 NEXT
python3 tools/pipeline.py run projects/<项目>       # 按固定顺序跑，遇到要处理的就停，并给出 NEXT
```

- **只照 NEXT 做**，不自己改顺序、不跳阶段、不手改生成出来的文件（`交付/01-资产提示词.md`、`交付/规格单/`、`交付/投喂包/` 都由脚本生成）。
- 唯一要创作的是把剧本拆成 `设定.json` 和 `分镜.json`（格式与拆法 `docs/19`，照 `examples/样例-便利店/` 写），写完 `python3 tools/storyboard.py check projects/<项目>` 改到通过。
- 唯一要"看"的是目检：逐张打开原图 / 白模拼图，按 `docs/18` 第四节清单判 pass / fix，用 `pipeline.py review` 记录。
- `confirm`、`approve` 只在用户在对话里明确确认 / 同意花费后才执行，不能替用户做。
- 出片后用 `python3 tools/pipeline.py qc projects/<项目> 第N条 <视频>` 审片，再按报告里的 24 分表看片打分；同一场连续的戏，用 `pipeline.py splice projects/<项目> 第N条 <视频>` 截尾帧后再 run，下一条自动接上。

要点：
- 交付物：`交付/01-资产提示词.md`（每张图标推荐出图模型）+ `交付/投喂包/<模型>/第N条/`，都由流水线生成；只出 A 白模预演版。用户只确认故事、剧本和花费。
- 用户项目放在 `projects/<项目名>/`；示例在 `projects/_示例-雨夜还钥匙/`。
- 手写模式（没有 JSON 数据的老项目）才手写 `交付/02-Seedance提示词.md`，写完跑 `python3 tools/prompt_lint.py projects/<项目>/交付/02-Seedance提示词.md --all`。
- 出图与出视频都是付费调用：只在用户明确要求并确认花费后执行；先 `--dry-run`。
- 密钥只放 `.env`（模板 `.env.example`），不得写进文档或提交。
- 始终用中文和用户交流。
