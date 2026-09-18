# 一词成剧 · One Word Series

**一个词进去，一整个系列出来 —— 连戏是写在文件里的，不是碰运气碰出来的。**

[English](README.md) · MIT · Python ≥ 3.10

```bash
pip install -e .
oneword rust --episodes 2
```

这条命令现在就能跑，不需要任何 key，不花一分钱，产出两个带字幕的成片 ——
机器上有 TTS 引擎的话，还带语音。

---

## 为什么要有这个东西

让任何「一句话生成视频」的流水线做一个系列：第 1 集还行，第 2 集夹克换了颜色，
第 3 集楼梯间长出一扇窗。因为世界的设定活在模型的 context 里，会衰减。

这个项目把设定放进文件。

```
词 ──► bible.json ──► 同样的锁定字节，注入每一集每一镜的每一条 prompt
```

`bible.json` 存的就是那些不能漂的东西：每个人物的 `locked_appearance`、
每个场景的 `locked_description`、视觉语法、连戏规则。模型**只写一次**。
之后每条 prompt 都是本地确定性拼出来的：

```
[STYLE] 35mm anamorphic, shallow depth of field; hard key from one practical source…
[LOCATION · Stairwell C] A concrete stairwell with painted green handrails, one
flickering tube light, numbered landing plates, and a steel fire door at the bottom.
[CHARACTER · Wen] Late thirties, wiry build, black hair cropped short and greying at
the temple, deep vertical scar through the left eyebrow, olive workwear jacket with a
torn left cuff, always carrying a brass key ring on the belt.

[ACTION] Wen forces the door open and finds the rust already inside
[CAMERA] medium shot, slow handheld drift
[CONTINUITY — MUST HOLD]
MUST: SER-01: C1's left jacket cuff is torn in every shot.
…
```

第 1 集和第 9 集拿到的是逐字节相同的那一段。有测试专门锁这件事，
一旦不同就挂。

## 闭环

```
分镜 ─► vendor.generate ──┐
                          │
            ┌── 审查 ◄────┘
            │
            ├─ 有 blocker？只重生成那一镜，最多两轮，
            │  永远不整集重跑
            ▼
   配音 + 字幕 + 合成 ─► episode-NN.mp4
```

## 产出

```
out/rust/
  bible.json             系列圣经：世界、人物、场景、连戏规则、分集大纲
  series-index.json      每集的状态与文件位置
  episode-01/
    episode-01.mp4       画面 + 语音 + 烧录字幕
    episode-01.srt
    episode-report.json  逐镜决策、每次生成、每次审查
    episode-report.html
    clips/               每镜每条 take，原样保留
```

## Vendor

| `--vendor` | 是什么 | 花钱 |
|---|---|---|
| `animatic`（默认） | 本地渲染的分镜卡 | 免费 |
| `seedance` | 火山方舟 即梦 / Seedance | 付费，需显式开启 |

默认 vendor **不是**模型画面，报告里每次都写明这一点。它存在的唯一理由是：
在花第一分钱之前，把整条链路真的跑通一遍。两个 vendor 满足同一个
`generate()` 契约，所以接一个新模型 = 写一个类。

### 换成真实出片

```bash
export ENABLE_VIDEO_GENERATION=1
export ARK_API_KEY=<火山方舟的 key>
export VIDEO_BUDGET_CNY=30

oneword rust --episodes 1 --vendor seedance
```

方舟兼容 OpenAI 协议，所以一把 key 可以同时喂写剧本的模型：

```bash
export LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
export LLM_API_KEY=$ARK_API_KEY
export LLM_MODEL=<你的文本模型 id>
```

配音默认 `--voice auto`：有什么用什么，一个都没有就静音出片，而不是让整部片子挂掉。
要指定就用 `--voice espeak`（本地、免费、机器音）、`--voice volc`（豆包语音，
需要 `VOLC_TTS_APPID` / `VOLC_TTS_TOKEN`）、或 `--voice silent`。

## 关于花钱的规矩

| | 自动重试 | 原因 |
|---|---|---|
| `POST` 提交任务 | **0 次** | 超时的提交可能已经建了付费任务，重发就是重复计费 |
| `GET` 轮询 / 取回 | ≤2 次 | 免费且幂等 |

* 不设 `ENABLE_VIDEO_GENERATION=1`，付费 vendor 根本构造不出来。
* 价目是白名单不是公式：没验过价的（模型，分辨率，时长）组合直接拒绝提交。
* `VIDEO_BUDGET_CNY` 在**提交之前**抛 `BudgetExceeded`，不是事后才发现。
* 单次运行只持有一个 vendor，报告校验会拒绝混供应商的记录。

## 关于说实话的规矩

这些由 `validate_episode_report()` 强制，不靠自觉：

* 非生成型 vendor 永远不能携带视觉结论 —— 没有模型看过这些像素，
  报告就不能写得像有。
* 两轮修完仍是 blocker 的镜头，状态写 `BLOCKERS REMAIN` 并列出镜号，
  不四舍五入成 `DELIVERED`。
* 只有 `severity: regenerate` 会再次花钱，且只花在那一镜上。
* 报告如果含有：混供应商、超过两轮、整集重跑、
  或任何没在上一轮被判为 blocker 却进入重生成的镜头 —— 直接拒绝。

## 更深的检查

这里的三帧审查是刻意做浅的。需要人工维护的规则包、因果叙事审计、
五级证据标签的话，装上这个项目的前身：

```bash
pip install "one-word-series[continuity]"
```

[Continuity-Agent](https://github.com/wargolet4ever/Continuity-Agent) ——
AI 短片连戏检查器，抓单帧看不出来的错。完全可选，这里没有任何东西依赖它。

## 还没做的

1. **首帧续接。** 现在每镜是独立文生视频。同场景连续镜头应该走图生视频，
   用上一镜末帧当首帧 —— `vendors.py` 已经留好位置了。
2. **音乐与环境声。** 现在只有对白轨。
3. **跨集审查。** 每集内部审了，但「第 3 集的楼梯间和第 1 集是不是同一个」
   还没有自动比对。这是剩下最该做的一件事。
4. **真实付费生成的证据。** 适配器写完了、mock 测过了，但还没出过一次付费成片。

## Windows 上的几件事

下面每一条都是降级而不是报错，所以一台裸 Windows 也能出片：

* **ffmpeg** 由依赖里的 `imageio-ffmpeg` 提供，不需要你单独装，也不用配 PATH。
* **espeak-ng** Windows 上默认没有，所以 `--voice auto` 会静音出片（字幕还在）
  并明确告诉你。想要本地配音就装 espeak-ng 并加进 PATH，想要真人声就用 `--voice volc`。
* **烧录字幕**需要带 libass 的 ffmpeg。你这台没有的话，`.mp4` 和 `.srt` 照样产出，
  播放器或剪辑软件里挂一下 `.srt` 就行。

## 测试

```bash
python -m unittest discover -s tests -t .
```

30 个测试，全部不碰付费 API。方舟适配器走 fake opener，
所以请求结构、提交不重试、预算上限这三件事都被断言了，且不花钱。
