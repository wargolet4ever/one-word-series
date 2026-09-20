# 一词成剧 · One Word Series

**一个词进去，一整个系列出来 —— 连戏是写在文件里的，不是碰运气碰出来的。**

[English](README.md) · MIT · Python ≥ 3.10

```bash
pip install -e .
oneword rust --episodes 2
```

这条命令现在就能跑，不需要任何 key，不花一分钱，产出两个带字幕的成片 ——
机器上有 TTS 引擎的话，还带语音。它会先问你要哪种风格，然后告诉你一件要紧事：

```
· no story was written for your word — this is the built-in placeholder.
  "rust" appears only in a few action lines. The cast, the locations,
  the beats and every line of dialogue are fixed strings shipped with this
  tool, and they are the same for every word anyone types.
  Set LLM_API_KEY / LLM_MODEL for a series actually written from "rust".
```

这句话要按字面理解：**没有写作模型时，你输入的那个词基本被忽略了。**
人物、场景、节拍、每一句台词，都是仓库里写死的字符串，谁输什么词都一样；
你的词只出现在少数几行动作描述里。你拿到的是整套机器真的跑通了 ——
真的 bible、真的锁定 prompt、真的连戏、真的一条成片 —— 但故事是演示故事。

免费看一遍值得，花钱拍它不值得。所以付费 vendor 在动手之前会先停下来问你：

```
  ────────────────────────────────────────────────────────────────
  You are about to pay seedance-ark for the placeholder story.

  Nothing here was written from "rust". You would be buying
  the demo — the same two characters, the same two rooms and the
  same dialogue everyone else gets, rendered in your chosen look.

  Cost if you continue: about ¥18.60 (10 clips at ¥1.86).
  ────────────────────────────────────────────────────────────────
  type yes to shoot the placeholder anyway:
```

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
the temple, a deep vertical scar splitting one eyebrow, olive workwear jacket with a
torn cuff, always carrying a brass key ring on the belt.

[ACTION] Wen forces the door open and finds the rust already inside
[CAMERA] medium shot, slow handheld drift
[CONTINUITY — MUST HOLD]
MUST: SER-01: C1's jacket cuff is torn through in every shot.
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

平台不只审 prompt，也审首帧。写实人脸在方舟的审核里会被判成真人照片，
直接拒绝提交。续接是锦上添花不是必需品，所以被拒时那一镜退回文生视频并明说 ——
一集已经付过钱的片子，不该因为一个增强功能被拒就丢掉：

```
  note: shot 4 was shot from text — the platform refused its first frame, so that cut may jump
```

被拒的提交不会建任务，所以那次尝试不花钱。

### 让同一个人一直是同一个人

bible 锁的是人物的**描述**，而描述指的是一类人，不是某一个人。
「三十多岁，精瘦，一道竖疤劈开一侧眉毛，橄榄色工装外套袖口撕裂」——
这能保证袖口不会自己补好，但挡不住文生视频每一镜都重新挑一张符合描述的脸：
第 2 镜戴眼镜，第 5 镜没戴，第 2 集换了下颌线。
这件事靠改 prompt 是修不好的，因为文字不是脸。

身份需要一张图：

```bash
oneword rust --vendor seedance                      # 一边拍一边认脸
oneword rust --cast-image C1=wen.jpg --vendor seedance
oneword rust --no-cast-images                       # 每一镜自己挑脸
oneword rust --reset-cast                           # 忘掉旧的，这次重新认
```

参考图会跟着这个人物出现的**每一镜**一起送出去。来源两种：你给，或者**自动采用**
—— 某个人物第一次**单独**出现的那一镜，就成为他的定妆照，然后被冻结。
和场景的基准帧是同一笔交易，冻结的理由也一样：每一集都重新认一次脸，
正是一部剧在每一步都看起来没问题的情况下悄悄换掉全部演员的方式。

```
  locked a face for Wen — every later shot is sent it
```

两个人同框的镜头永远不会被用来认脸 —— 没法说哪张脸是谁的。
另外，`noir` 里拍的定妆照不是 `anime` 里的同一个人，所以改风格之后
旧的参考图会被放到一边，而不是和新风格对着干：

```
· the locked portraits were shot in noir, not anime — setting them aside for this run.
  Add --reset-cast to adopt new ones in this look, or --style to go back.
```

首帧和参考图是**互斥的** —— 方舟拒绝同时带着两者的请求。所以这是一道选择题，
而**有首帧的时候首帧赢**：它递过去的那一帧里本来就有上一镜确立的那张脸，
于是它同时带住了房间**和**脸；参考图只带得住脸。参考图留给续接够不到的镜头 ——
一个场景的第一镜、一集的第一镜 —— 那里身份没有别的东西托着。

审核在这里更严：一张好的写实定妆照，恰恰就是最像真人照片的东西。
输入图被拒时退回纯文本，而不是丢掉这一镜；报告里写明它实际是在哪一级拍成的：

```
  note: shot 4 was shot without the cast portraits — the platform refused them,
  so that face may differ
```

非写实风格（`anime`、`storybook`、`stopmotion`）被拒的概率低得多，
这本身就是选择用这类风格拍一整部剧的一个实在理由。

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

风格是整部剧锁死的东西，所以选它的时机是在第一镜之前 ——
而不是十条片子都拍出来之后才发现没人选过这个 look。交互式运行会先问：

```
Pick a look. It is locked for the whole series.

  1) 16mm         16mm film
                  photographic, 16mm film stock with visible grain and gate weave
  2) analog       Analog video
  …
  0) let the writing model choose (whatever it imagines, unlocked)

  style [1]:
```

给了 `--style` 就不问。给 `--yes` 则什么都不问 —— 定时任务要的就是这个。

```bash
oneword styles                              # 看有哪些
oneword rust --style noir                   # 整部剧用黑白拍
oneword rust --style ./my-look.json         # 自己的，键和预设一样
```

内置八种：`documentary` `noir` `anime` `storybook` `16mm` `clinical`
`analog` `stopmotion`。每一种先点名**媒介** —— 实拍、赛璐珞动画、水彩 ——
再是镜头、布光、色板、基调，以及这个look绝对不能出现什么。

把两种 look 合起来是一次书面选择，不是一个开关。`16mm` 要暖色褪色片基，
`noir` 要完全无彩色 —— 用一个覆盖另一个，结果就只是后面那个。所以
`styles/16mm-noir.json` 是逐字段挑的：片基和颗粒取自前者，光和色板取自后者：

```bash
oneword rust --style styles/16mm-noir.json
```

想写自己的，拿它当起点复制一份。

**风格不影响一条片子的价格。** 计费只看分辨率和时长，prompt 内容不进这个账。
挑你真正想要的 look，要省钱就去压 `SEEDANCE_RESOLUTION`。

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

这也是唯一能告诉你「参考图到底起没起作用」的东西，所以它才值得配一个。
当某个人物有锁定的定妆照时，漂移检查比对的是**定妆照本身**，而不是他第一次
出现的那一镜的某一帧——那张图才是真正被送给后面每一镜的图，比对它问的才是
那个该问的问题。另外，被平台拒过参考图的镜头会被标出来，所以那一镜里的脸不一致
读起来是「这一镜机制根本没碰到」，而不是「机制失效了」。

本地初筛是分诊，不是判决。它的阈值是量出来的不是拍出来的，而测量结果说两个
分布是重叠的：大约 80% 的比较落进一个初筛拒绝独自决定的 review 区间，交给多模态
比对——后者读的是写下来的事实（袖口有没有破、扶手是不是绿的），不是数像素。
[`docs/drift-calibration.md`](docs/drift-calibration.md) 里有全部数字和复现命令。

### 哪些设定锁得住，哪些锁不住

第一次用视觉模型跑真实素材，出了 7 条 DRIFTED。把模型实际看到的东西读一遍，
有一个模式藏不住：

| 锁定的事实 | 被判违反 | 模型实际看到的 |
|---|---|---|
| 挂钟显示 4:10 | 6 次 | 10:52、8:25、8:25、7:35、7:35、10:10 |
| **左**眉上的疤 | 4 次 | 右脸颊、额头、额头、右眉 |
| **左**袖口撕裂 | 5 次 | 卷起、卷起、卷起、卷起、没破 |
| C2 的红色领针 | 4 次 | 没有、没有、没有、深色纽扣 |
| **只有一个**暖光源 | 3 次 | 窗光+环境光、冷色荧光、窗光+壁灯 |
| **扶手是绿色的** | **0 次** | *—— 一次都没被判违反 ——* |

最后一行是对照组：一个大面积、有颜色、属于建筑本身的事实，每一镜都守住了。
失败的全是**小的、分左右的、要读数的、要计数的**。而挂钟比这还糟：
**没有任何视频模型能把表盘画成你指定的时间**，所以那条规则注定在那个房间的
每一镜、永远失败。而它就写在这个仓库自带的模板里。

这才是"锁不住的设定"的真实代价。7 条里有 6 条是圣经自己提出的不可能要求，
而唯一那条真正的发现 —— 夹克变成了衬衫 —— 被工具自己制造的噪音埋掉了。

所以现在圣经在开拍之前就会被检查：

```
· 2 locked fact(s) a generator is unlikely to ever hold, so they will read
  as drift in every episode:
    SER-03 [readable-value]
      The wall clock in Unit 704 always reads 4:10.
      → say the clock is stopped and unlit, not what time it shows
  These are not caught by spending more; they are caught by rewording.
```

同一份清单也写进了给写剧本模型的 prompt，所以从你的词生成的圣经也会避开它们；
漂移报告里引用了这类事实的差异会被标注出来 —— 属实，但不是新闻。
分类和每一类的来历在 `oneword/lockability.py`。

### 这一条到底为什么漂了

距离只告诉你两帧差多远，不告诉你这一镜是**怎么拍的**——而后者早就写在盘上了：
每一集的报告都记着这一镜有没有续接上一镜、首帧有没有被平台拒掉、参考图有没有
送出去。把这两份文件 join 起来不花一分钱：

```bash
python scripts/explain_drift.py out/rust
```

```
ep  shot  subject          verdict        comp    col    str  how it was shot
 1     2  Stairwell C      CONSISTENT    0.010  0.012  0.008  chained (from shot 1)
 2     2  Stairwell C      REVIEW        0.075  0.109  0.013  chained (from shot 1)  vs shot 1: 0.010
 2     4  Unit 704         DRIFTED       0.123  0.186  0.007  chain refused: InputImage…
```

真实素材就是靠这个发现了一件事：**续接的镜头像的是它续的那一镜，不是系列基准**，
而且它会原原本本继承那一镜已经丢掉的东西。第一次真实运行里，一个续接镜头离基准
0.010，另一个离基准 0.075 —— 因为后者忠实地续上了一个已经漂了的镜头。

所以该读的是两个数，不是一个：

| | 回答什么 |
|---|---|
| 离**基准**多远 | 整部剧还守住了吗？ |
| 离**前一镜**多远 | 这一刀接住了吗？ |

贴着前一镜、却离基准很远 —— 意思是这一整段是整体位移了。这时候去重生成其中
任何一镜，都是在重拍一个本来就和邻居严丝合缝的镜头；真正需要重新对齐基准的是
**整集**。报告看到这个形态时会直接这么写，而不是把一集的漂移记成四个互不相干的坏镜头。

同一次运行还会报**每一行是被哪个通道决定的**。真实剧集素材里房间确实是同一个
房间，所以结构通道理所当然几乎不动，颜色扛下了全部判定 —— 也就是说这个
composite 实际上是个单通道测量，只是顶着双通道的名字。现在它会自己说出来，
而不是让你默认它是两个通道一起算的。

等你有素材了，就用自己的素材去量：

```bash
python scripts/calibrate_drift.py --series out/rust
```

某个总体素材太少分不开时，它会直说，而不是硬吐一个数字出来。

### 不为同一条片子付两次钱

一次付费运行在第 8 镜挂掉，硬盘上已经躺着 7 条成片。默认情况下，下一次运行直接用它们：

```bash
oneword rust --vendor seedance --out out-real     # 从断掉的地方接着走
oneword rust --vendor seedance --fresh            # 全部重新买一遍
```

每条生成出来的 clip 旁边会落一个小 JSON，记着它是从哪条 prompt、哪个 vendor
生成的，花了多少钱。只有当下一次运行会问一模一样的东西时才复用 ——
prompt 指纹相同、vendor 相同、文件还读得出来。

这一条规则覆盖了所有值得覆盖的情况，一个都不用特判：改了圣经、换了风格、
换了模型或分辨率 —— prompt 变了，指纹对不上，那一镜就重做。一次「悄悄留用了
上一个 look 的片子」的续跑，代价远高于重买一条。

运行结束会说清楚跳过了什么、值多少钱：

```
  reused 7 shot(s) already on disk, saving ¥13.02
```

### 一条片子多少钱

预算护栏只有在背后的数字是真的时候才有意义，所以价目**不写在源码里**。
它按账户、按模型、按区域不同，而且会变；写死在代码里的数字对某些人必然是错的，
而且升级会把用户改对的值悄悄覆盖回去。

```bash
export ONEWORD_PRICE=1.86        # 只管这一次运行
cp prices.example.json prices.json   # 或者长期放着
```

`prices.json` 的结构是 模型 → 分辨率 → 时长 → 元：

```json
{ "doubao-seedance-2-0-mini-260615": { "480p": { "5": 1.86 } } }
```

表里没有的组合会在提交任何东西之前被拒绝，报错里直接给出要填的那一行。

## 关于花钱的规矩

| | 自动重试 | 原因 |
|---|---|---|
| `POST` 提交任务 | **0 次** | 超时的提交可能已经建了付费任务，重发就是重复计费 |
| `GET` 轮询 / 取回 | ≤2 次 | 免费且幂等 |

* 不设 `ENABLE_VIDEO_GENERATION=1`，付费 vendor 根本构造不出来。
* 价目是你自己提供的白名单，既不是公式，也不是随包发的猜测值：
  表里没有的（模型，分辨率，时长）组合直接拒绝提交。
* `VIDEO_BUDGET_CNY` 在**提交之前**抛 `BudgetExceeded`，不是事后才发现。
* 单次运行只持有一个 vendor，报告校验会拒绝混供应商的记录。
* 付费 vendor 不会在没被明确要求的情况下去拍那个演示故事。它会先打印
  这一趟要花多少钱、拍出来的到底是什么，然后等你输入 `yes`。
  退出码 10 表示你说了不 —— 什么都没生成，什么都没扣。

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

* **用视觉模型看过真实素材**，结果发现它自己报的 7 条漂移里有 6 条是
  圣经提出的不可能要求，而不是生成器的失败 —— 其中包括这个仓库自带的一条规则：
  要求每一条 clip 里的挂钟都显示 4:10。`oneword/lockability.py` 和 README 里那张表
  就是读模型实际看到了什么读出来的。
* **对真实素材跑过一次跨集漂移检查，并且推翻了这个仓库里写下的一个猜测。**
  标定文档当初预测：锁定风格的剧集变化应该比合成靶场**小**。真实的两集变化
  更**大** —— 六个同场景读数里有四个超过了靶场同场景的最差配对。同一次检查
  还显示结构几乎不动、颜色扛下了全部判定。这两件事现在都会被报出来而不是
  被默认，阈值也按总体拆开了。六个读数在
  [`docs/drift-calibration.md`](docs/drift-calibration.md) 里。

其余部分 —— 真实故障下的修复环、人物比对 —— 靠测试和离线 vendor 覆盖，
那和「生产环境验证过」不是一回事。凡是这个区别重要的地方，文中都就地写明了。

## 还没做的

1. **参考图到底有没有用。** 视觉模型现在已经确认了它要解决的那个问题确实存在 ——
   同一个人物的疤在四个镜头里跑到了脸颊、额头和另一边眉毛 —— 但那批素材是在
   参考图机制之前拍的，整个过程没送过任何定妆照。修复本身仍然没有被测量过。
2. **用真实素材量出来的阈值。** 现在的阈值仍然来自合成靶场，而第一次真实运行
   证明靶场比现实**更松**而不是更严 —— 一个根本没变过的房间读出了 0.072 和
   0.123。`scripts/calibrate_drift.py --series` 能量真实素材，但数字还没动，
   因为两集构不成一个分布。
3. **仓库里放一条演示片。** 付费成片已经有了，还没有一条提交进来。
4. **人物参考图还没在真实 API 上跑过。** 请求结构、降级阶梯、冻结规则
   都有测试覆盖，走的是 fake opener。方舟的审核会不会接受一张**它自己生成的**
   画面里截出来的脸，这件事还没花钱验证过。退回纯文本那条路大概率会被触发 ——
   它本来就是为这件事写的。

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

270 个测试，全部不碰付费 API。方舟适配器走 fake opener，
所以请求结构、提交不重试、预算上限这三件事都被断言了，且不花钱。
