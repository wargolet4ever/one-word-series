# One word → a series

## 一句话

这个仓库从「验证器」推成「出片器」：一个词进去，一本**系列圣经**被写下来，
之后每一集都是对着这本圣经拍的——而不是对着模型上一次碰巧记住的东西拍的。

The whole difference from a one-prompt pipeline is where the facts live.
A one-prompt pipeline keeps them in the model's context, so by episode three the
jacket has changed colour. This keeps them in a file, and injects the same bytes
into every prompt of every episode. Continuity stops being luck.

```
word ─► bible.json ─► episode beats ─► shots ─► prompts
                                         │
                                         ├─► vendor.generate ──┐
                                         │                     │
                                         │   ┌── audit ◄───────┘
                                         │   │
                                         │   ├─ blockers? regenerate ONLY those,
                                         │   │  at most twice, never the whole episode
                                         │   ▼
                                         └─► speech + subtitles + assembly
                                                     │
                                                     ▼
                                            episode-NN.mp4 + .srt + report
```

## 跑一次（零成本，现在就能跑）

```bash
pip install -e .
apt install espeak-ng          # macOS: brew install espeak-ng
oneword rust --episodes 2 --shots 5
```

产出 `out/rust/`：

```
bible.json                 系列圣经：世界、人物锁定外观、场景锁定描述、连戏规则、分集
series-index.json          每集的状态与文件位置
episode-01/
  episode-01.mp4           成片：画面 + 语音 + 烧录字幕
  episode-01.srt
  episode-report.json      逐镜决策、每次生成、每次审查、证据等级
  episode-report.html
  clips/                   每镜每条 take，原样保留
```

默认 vendor 是 `local-animatic`：本地 ffmpeg 渲染的分镜卡，**不是模型画面**。
报告里明写这一点。它存在的唯一理由是：在花第一分钱之前，把
「规划 → 生成 → 审查 → 只补问题镜 → 合成」这条链路真的跑通一遍。

## 换成真实出片（即梦 / Seedance）

```bash
export ENABLE_VIDEO_GENERATION=1
export ARK_API_KEY=<火山方舟的 key>
export SEEDANCE_MODEL=doubao-seedance-1-0-lite-t2v-250428
export VIDEO_BUDGET_CNY=30

oneword rust --episodes 1 --vendor seedance
```

写剧本的模型走仓库原有的三个变量。方舟本身兼容 OpenAI 协议，所以一把 key 可以同时喂两边：

```bash
export LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
export LLM_API_KEY=$ARK_API_KEY
export LLM_MODEL=<你的文本模型 endpoint 或 model id>
```

配音可选 `--voice volc`（豆包语音 HTTP v1），需要 `VOLC_TTS_APPID` / `VOLC_TTS_TOKEN`。
不配就用 `--voice espeak`（本地、免费、机器音），或 `--voice silent`。

### 花钱这件事，规矩和仓库其余部分一致

| | 自动重试 | 原因 |
|---|---|---|
| `POST` 提交任务 | **0 次** | 超时的提交可能已经建了付费任务，重发就是重复计费 |
| `GET` 轮询 / 取回 | ≤2 次 | 免费且幂等 |

* `ENABLE_VIDEO_GENERATION=1` 不设置，付费 vendor 根本构造不出来。
* 价目是白名单不是公式：`PRICE_CNY` 里没有的（模型，分辨率，时长）组合直接拒绝提交，
  而不是猜一个数字。
* `VIDEO_BUDGET_CNY` 在超出时抛 `BudgetExceeded`，**在提交之前**。
* 单次运行只持有一个 vendor，报告校验会拒绝混供应商的记录。

## 圣经里哪些字段是「锁死」的

| 字段 | 谁写 | 之后谁能改 |
|---|---|---|
| `characters[*].locked_appearance` | 模型写一次 | 没人。逐字注入每一集每一镜 |
| `locations[*].locked_description` | 模型写一次 | 同上 |
| `visual_grammar` | 模型写一次 | 同上 |
| `continuity_rules` | 模型写一次 | 编译成每镜 `MUST:` 行 |
| `episodes[*].beats` | 模型写 | 每集可以重写，但只能引用已存在的人物与场景 id |

`validate()` 会把模型编出来的未知 id（`L99`、`C42`）拉回圣经里已有的 id——
跨集漂移正是这一层存在的理由，所以它在源头就被修掉，而不是传下去。

`bible.locked_block()` 是纯函数：同样的 id 进去，永远是同样的字节出来。
`test_every_episode_shares_the_same_locked_strings` 锁住这一点。

## 审查与修补

* 非生成型 vendor（分镜卡）一律降级为 `RULE TRIAGE ONLY`，**不允许**带视觉结论——
  没有模型看过这些像素，报告就不能写得像有。
* `FrameContinuityAuditor` 抽 3 个有序帧，连同锁定描述一起交给多模态模型，
  只接受「能在帧上看见」的违规。抽帧失败或没配 key → 返回空 + 如实标 `RULE TRIAGE ONLY`。
* 只有 `severity == regenerate` 才触发重生成，且**只重生成那一镜**。
* 上限两轮。两轮后仍然是 blocker 的镜头，状态写 `BLOCKERS REMAIN` 并列出镜号，
  不四舍五入成 `DELIVERED`。

`validate_episode_report()` 会拒绝：混供应商、超过两轮、整集重跑、
以及任何没有在上一轮被判为 blocker 却进入重生成的镜头。

## 还没做的

1. **首帧续接。** 现在每镜是独立文生视频。同场景连续镜头应该走图生视频，
   用上一镜末帧当 `first_frame`——`vendors.py` 的 content 数组已经留好位置了。
2. **音乐与环境声。** 现在只有对白轨。
3. **跨集审查。** 每集内部审了，但「第 3 集的楼梯间和第 1 集是不是同一个楼梯间」
   还没有自动比对。这是这个项目最该做的下一件事，也是别人最难抄的一件事。
4. **真实付费生成的证据。** 适配器写完了、mock 测过了，但仍未完成一次真实出片。
