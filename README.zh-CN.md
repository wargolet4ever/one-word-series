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

### 当 clip 自己已经有声音

视频模型现在会带着对白和环境声一起返回。在上面再压一层旁白通常是降级，
所以 `--audio` 决定谁说了算：

| `--audio` | 行为 |
|---|---|
| `mix`（默认） | clip 自带音轨压低约 9 dB 垫在旁白下面，两者都保留 |
| `keep` | clip 自带的就是全部，完全不做旁白 |
| `replace` | 只要旁白，丢掉 clip 自带音轨 |

自带人声的镜头根本不会触发 TTS 调用，所以不会出现两个声音抢着说同一句。
无声的 clip 在三种模式下表现完全一致。

### 这句台词由谁来说

vendor 能出声的时候，台词会进 prompt，由视频模型自己演 —— 有口型、在角色身上、
在那个房间的声学环境里。外挂 TTS 没法比，因为 TTS 是在一段表演之上朗读，
而不是表演本身。

```bash
oneword rust --vendor seedance --audio keep     # 模型自己演，没人往上配音
oneword rust --dialogue off                     # 台词不进 prompt
```

`--dialogue auto`（默认）在 vendor 能生成音频时开启，对只出无声片的 vendor 关闭 ——
那种情况下台词进 prompt 什么也换不来。

让视频模型说台词的典型翻车是：它把那句话写在画面上而不是说出来。所以 dialogue 块
会点名说话人、明确「说出声」，并且只在这些镜头上给负面提示追加
`subtitles, captions, burned-in text, …`。字幕仍然写进 `.srt` ——
画面里听见，字幕文件里读到。

### 让一镜接着上一镜

同一个房间里的两个连续镜头，如果都只从文字生成，就是对那个房间的两次独立猜测 ——
所以中间那刀会跳。圣经能防止房间变成**另一个房间**，但没法让一镜接上另一镜。
图生视频可以：

```bash
oneword rust --vendor seedance          # vendor 支持首帧时默认开启
oneword rust --chain off                # 每镜独立生成
```

上一镜的末帧变成下一镜的首帧，以 base64 内联发送 —— 因为那一帧在你机器上，
方舟够不到本地路径。

只有真正连续的镜头才会续接：**同一场景**、**在剪辑上相邻**。跨场景续接会把
错误的房间贴进首帧，而模型会老老实实地保留它。

有一种情况工具拒绝和稀泥。如果某一镜在后面的镜头已经从它续接之后才被修复，
那后面那一镜续的就是一条已经不在成片里的 take。重新生成下游会花掉没人批准的钱 ——
只有 blocker 才有资格再花钱 —— 所以它会被报成 stale，交给你判断：

```
  note: shot 3 continues shot 2, which was regenerated afterwards — that cut may jump
```

### 音乐与环境声

```bash
oneword rust --music score.mp3              # 给每一集垫一层
oneword rust --music score.mp3 --music-db -14
```

垫乐是在剪完之后、贯穿整集加上去的，**不是**逐镜加：一条在每个切点重新开始的
配乐，是让剪辑听起来像幻灯片最可靠的办法。短的音轨会自动循环；画面流是直接
复制的，不重新编码。

它会自己让路。`sidechaincompress` 用整集自身的声音去驱动音乐的增益，
有人说话时垫乐退后，停下来时回来 —— 固定音量要么压住对白，要么其他地方根本听不见。
ffmpeg 没有 sidechain 滤镜时退回固定音量，并明确告诉你用的是哪种。
音轨读不出来只会丢掉配乐，不会丢掉成片。

它**不生成**音乐，只混合你给的文件。生成配乐是一次有成本、有版权问题的模型调用，
这两件事都不该藏在一个看起来像调音台的开关后面。

## 选择风格

```bash
oneword styles                              # 看有哪些
oneword rust --style noir                   # 整部剧用黑白拍
oneword rust --style ./my-look.json         # 自己的，键和预设一样
```

内置八种：`documentary` `noir` `anime` `storybook` `16mm` `clinical`
`analog` `stopmotion`。每一种先点名**媒介** —— 实拍、赛璐珞动画、水彩 ——
再是镜头、布光、色板、基调，以及这个look绝对不能出现什么。

### 风格是锁定项，不是旋钮

这个工具的全部论点是「画面不会漂」。一个可以随手改的风格会直接摧毁这个论点，
所以风格是选一次、写进圣经、逐字节注入每一集每一镜的 —— 和人物那条破袖口
完全同等的待遇。

给已有的剧换风格是允许的，但那是一次显式决定：

```bash
oneword rust --bible out/rust/bible.json --style anime --reset-references
```

它只重写风格块，**人物、场景、分集节拍一律不动** —— 换媒介不换人。
这部剧重画成动画之后，那条袖口还是破的。

### 为什么必须重设基准

每一张基准帧都记着自己是在哪个风格下拍的。换了 look，每一帧都合法地和旧的不同，
所以跨风格比对会把一部好好的剧报成散架了 —— 而实际上你只是换了个预设。
`oneword drift` 拒绝出这种报告：

```
the references were shot in noir but this bible is now anime. Every frame
differs by design, so a comparison would report drift that is not drift.
  Re-base them deliberately: oneword drift <dir> --reset-references
```

## 跨集漂移

上面所有东西都在一集之内工作。这一节是唯一跨集看的部分，也是「一句话生成」
那类流水线做不了的部分：把第 9 集和第 1 集比，你得有一个写下来的基准，
而把设定放在 context 里的流水线没有东西可指。

```bash
oneword drift out/rust          # 多集运行结束后也会自动跑一遍
```

某个场景或人物第一次出现并通过时，那一帧就被采纳为它的**基准帧**，写进
`references/`。之后每一次出现——任何后续集数、任何后续运行——都拿去和
**同一张基准帧**比，而不是和上一集比。

这个区别就是整个设计。拿每集和上一集比，是注定慢性失败的那种安排：第 2 集
漂了 3% 通过了，然后它成了新基准；第 3 集相对**它**再漂 3%，也通过；到第 9 集
已经和第 1 集毫无关系，而一路上每一次检查都是绿的。所以基准一旦采纳就冻结。
重设基准是有的，`--reset-references`，但它会连同触发它的集数一起记进 registry ——
重设基准是关于这部剧的决定，不是顺手的杂务。

### 它能判什么，不能判什么

| 对象 | 本地初筛 | 原因 |
|---|---|---|
| 场景 location | 是 | 画面主体就是场景，所以一个廉价指纹确实说明问题 |
| 人物 character | **否** | 人只占画面一部分，且和一个本就该变的场景共享画面；整帧指纹量的其实是房间 |

没配多模态模型时，人物漂移一律报 `NOT CHECKED`——**不报通过**——整部剧的状态
写 `PARTIAL` 而不是 `CONSISTENT`。没人挣来的那个绿勾，比一个诚实的空白更糟。

本地初筛是分诊，不是判决。它的阈值是量出来的不是拍出来的，而测量结果说两个
分布是重叠的：大约 80% 的比较落进一个初筛拒绝独自决定的 review 区间，交给多模态
比对——后者读的是写下来的事实（袖口有没有破、扶手是不是绿的），不是数像素。
[`docs/drift-calibration.md`](docs/drift-calibration.md) 里有全部数字和复现命令。

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

## 哪些是真的跑过的

README 里的说法都是代码确实会做的事。这一节是更窄的一组：真的花钱、
对着线上接口做成过的事。

* **一次完整的付费 Seedance 出片。** 从圣经拼出 prompt、提交方舟、轮询、下载、
  审查、合成为成片。不是 mock。
* **花钱护栏在它该管的那条路上生效过。** 价目白名单拒绝过表外组合，
  预算上限是在提交之前校验的，不是事后。
* **只有真实接口才会产生的故障。** 方舟对未开通或拼错的 model id 返回 404，
  真正的原因在响应体里 —— 适配器现在会把它显示出来，而不是干巴巴一句
  `HTTP Error 404`。单镜生成要几分钟，所以现在会报进度而不是静默。

其余部分 —— 漂移阈值、真实故障下的修复环 —— 靠测试和离线 vendor 覆盖，
那和「生产环境验证过」不是一回事。凡是这个区别重要的地方，文中都就地写明了。

## 还没做的

1. **没有模型时的人物漂移。** 场景有本地初筛，人物必须有多模态 key，
   否则如实留空不检查。
2. **用真实素材重新标定阈值。** 现在的阈值标定在合成靶场上。拿
   `scripts/calibrate_drift.py` 对着真实 Seedance 成片重跑一遍再改常数 ——
   现在已经具备条件，但还没做。
3. **仓库里放一条演示片。** 付费成片已经有了，还没有一条提交进来。

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

116 个测试，全部不碰付费 API。方舟适配器走 fake opener，
所以请求结构、提交不重试、预算上限这三件事都被断言了，且不花钱。
