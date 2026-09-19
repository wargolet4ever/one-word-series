# One Word Series · 一词成剧

**One word in, a whole series out — with the continuity written down.**

[中文](README.zh-CN.md) · MIT · Python ≥ 3.10

```bash
pip install -e .
oneword rust --episodes 2
```

That runs right now, with no API key and no spend, and hands you two finished
`.mp4` files with subtitles — and speech too, if this machine has a TTS engine.
It asks you which look you want, and then tells you something important:

```
· no story was written for your word — this is the built-in placeholder.
  "rust" appears only in a few action lines. The cast, the locations,
  the beats and every line of dialogue are fixed strings shipped with this
  tool, and they are the same for every word anyone types.
  Set LLM_API_KEY / LLM_MODEL for a series actually written from "rust".
```

Read that literally. Without a writing model, **your word is nearly ignored**.
What you get is the machinery working end to end — a real bible, real locked
prompts, real continuity, a real film — around a demo story. That is worth
seeing for free, and it is not worth paying for, which is why a paid vendor
will not shoot it without asking you first:

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

## The problem this exists for

Ask any one-prompt video pipeline for a series and you get episode 1, then
episode 2 where the jacket is a different colour, then episode 3 in a stairwell
that has grown a window. The facts of the world live in the model's context, so
they decay.

This puts them in a file instead.

```
word ──► bible.json ──► the same locked bytes injected into every prompt
                        of every shot of every episode
```

`bible.json` holds the things that must not drift — each character's
`locked_appearance`, each location's `locked_description`, the visual grammar,
the continuity rules. A model writes it **once**. After that, every prompt is
assembled locally and deterministically:

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

Episode 1 and episode 9 get that block byte for byte. There is a test that
fails if they ever don't.

## The loop

```
shots ─► vendor.generate ──┐
                           │
             ┌── audit ◄───┘
             │
             ├─ blockers? regenerate ONLY those, at most twice,
             │  never the whole episode
             ▼
   speech + subtitles + assembly ─► episode-NN.mp4
```

## What you get

```
out/rust/
  bible.json             the series: world, cast, locations, rules, episode plan
  series-index.json      every episode's status and files
  episode-01/
    episode-01.mp4       picture + speech + burned subtitles
    episode-01.srt
    episode-report.json  per-shot decisions, every generation, every audit
    episode-report.html
    clips/               every take of every shot, kept
```

## Vendors

| `--vendor` | what it is | cost |
|---|---|---|
| `animatic` *(default)* | locally rendered storyboard cards | free |
| `seedance` | Volcengine Ark 即梦 / Seedance | paid, opt-in |

The default vendor is **not** model output, and every report says so. It exists
so you can prove the whole loop works before spending anything. Both vendors
satisfy one `generate()` contract, so adding a model means writing one class.

### Going paid

```bash
export ENABLE_VIDEO_GENERATION=1
export ARK_API_KEY=<your Ark key>
export VIDEO_BUDGET_CNY=30

oneword rust --episodes 1 --vendor seedance
```

Ark speaks the OpenAI protocol, so one key can drive the writing model too:

```bash
export LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
export LLM_API_KEY=$ARK_API_KEY
export LLM_MODEL=<your text model id>
```

Speech is `--voice auto` by default: it takes the best engine the machine has
and falls back to silence rather than losing the film. Force one with
`--voice espeak` (local, free, robotic), `--voice volc` (豆包语音,
needs `VOLC_TTS_APPID` / `VOLC_TTS_TOKEN`), or `--voice silent`.

### When the clip already has sound

Video models return clips with dialogue and room tone in them. Narrating over
that is usually a downgrade, so `--audio` decides what wins:

| `--audio` | what happens |
|---|---|
| `mix` *(default)* | the clip's own audio is ducked ~9 dB under the narration; both survive |
| `keep` | the clip's audio is the audio, narration is skipped entirely |
| `replace` | narration only — the clip's own track is discarded |

A shot whose clip already speaks gets no TTS call at all, so nothing dubs a
second voice over the first. A silent clip behaves identically under all three.

### Who says the line

When the vendor can make sound, the line goes into the prompt and the video
model performs it — lip-synced, in character, in the room's own acoustics. No
external TTS competes with that, because TTS reads over a performance instead
of being one.

```bash
oneword rust --vendor seedance --audio keep     # the model acts, nothing dubs over it
oneword rust --dialogue off                     # no line in the prompt
```

`--dialogue auto` (the default) turns this on exactly when the vendor
generates audio, and off for a vendor that returns silent clips, where a line
in the prompt buys nothing.

The failure mode of asking a video model for dialogue is that it writes the
words across the frame instead of speaking them, so the dialogue block names
the speaker, says the words are spoken aloud, and the negative prompt gains
`subtitles, captions, burned-in text, …` on exactly those shots. Subtitles are
still written to the `.srt` — heard in the picture, read from the sidecar.

### Continuing a shot from the last one

Two consecutive shots in one room, generated from text alone, are two
independent guesses at that room — so the cut between them jumps. The bible
stops the room becoming a *different* room; it cannot make one shot continue
the other. Image-to-video can:

```bash
oneword rust --vendor seedance          # on by default where the vendor takes a first frame
oneword rust --chain off                # every shot generated independently
```

The last frame of the previous shot becomes the first frame of the next, sent
inline as base64 because the frame is on your machine and Ark cannot reach a
local path.

Chaining only happens where the shots really are continuous: **same location**,
**adjacent in the cut**. Chaining across a cut to another room would paste the
wrong room into the first frame, and the model would obediently keep it.

There is one case the tool refuses to paper over. If a shot is repaired after
the next shot was chained from it, that next shot now continues a take which is
no longer in the episode. Regenerating the dependents would spend money nobody
approved — only a blocker spends again — so the chain is reported stale and
left for you:

```
  note: shot 3 continues shot 2, which was regenerated afterwards — that cut may jump
```

Platforms moderate the first frame as well as the prompt. A photorealistic
face in a frame reads to Ark's moderation as a real photograph, and it refuses
the submission. Continuing from the last frame is an improvement, not a
requirement, so a refusal falls back to text-to-video for that shot and says
so — an episode you have already paid for is not lost because an enhancement
was declined:

```
  note: shot 4 was shot from text — the platform refused its first frame, so that cut may jump
```

A refused submit creates no task, so the attempt costs nothing.

### The same face, shot after shot

The bible locks a character's **description**, and a description names a
*type*, not a person. "Late thirties, wiry, a scar through the left eyebrow,
an olive jacket with a torn left cuff" stops the cuff mending itself between
episodes — and a text-to-video model still casts a new face that fits it every
single time. Glasses in shot 2, no glasses in shot 5, a different jaw in
episode 2. No amount of prompt writing fixes that, because words are not a
face.

Identity needs a picture:

```bash
oneword rust --vendor seedance                      # adopts faces as it goes
oneword rust --cast-image C1=wen.jpg --vendor seedance
oneword rust --no-cast-images                       # every shot casts its own face
oneword rust --reset-cast                           # forget them and adopt again
```

A portrait is sent with every shot that character appears in. It is either
supplied by you or **adopted**: the first shot a character appears in *alone*
becomes their portrait, and it is then frozen — the same bargain the reference
stills make for locations. Frozen matters for the same reason: re-adopting a
face every episode is exactly how a series becomes a different cast while each
individual step looks fine.

```
  locked a face for Wen — every later shot is sent it
```

A shot with two people in it is never adopted from; there would be no way to
say which face was whose. And a portrait shot in `noir` is not that character
in `anime`, so after a restyle the old portraits are set aside rather than
fighting the new look:

```
· the locked portraits were shot in noir, not anime — setting them aside for this run.
  Add --reset-cast to adopt new ones in this look, or --style to go back.
```

Moderation applies here too, and harder: a good photorealistic portrait is
exactly what reads as a photograph of a real person. The submit degrades one
rung at a time — first frame plus portraits, then portraits alone, then text —
and the report says which rung the shot was actually made on:

```
  note: shot 4 was shot without the cast portraits — the platform refused them,
  so that face may differ
```

Non-photographic styles (`anime`, `storybook`, `stopmotion`) are refused far
less often, which is a real argument for shooting a series in one.

### Music and ambience

```bash
oneword rust --music score.mp3              # a bed under every episode
oneword rust --music score.mp3 --music-db -14
```

The bed goes on **after** the cut, across the whole episode, never per shot: a
score that restarts at every cut is the most reliable way to make an edit feel
like a slideshow. A short track loops; the picture stream is copied, not
re-encoded.

It ducks itself. `sidechaincompress` lets the episode's own audio drive the
music's gain, so the bed steps back when someone speaks and comes up when they
stop — a fixed level is either too loud under dialogue or inaudible everywhere
else. Where the ffmpeg build has no sidechain filter it uses a fixed level and
says which one you got. A track that cannot be read costs you the score, never
the episode.

This does not *generate* music. It mixes a file you supply, because generating
a score is a model call with its own cost and its own licensing questions, and
neither belongs behind a flag that looks like an audio mixer.

## Choosing a look

The look is locked for the life of the series, so the moment to choose it is
before the first shot — not after ten clips arrive in a look nobody picked.
An interactive run asks:

```
Pick a look. It is locked for the whole series.

  1) 16mm         16mm film
                  photographic, 16mm film stock with visible grain and gate weave
  2) analog       Analog video
  …
  0) let the writing model choose (whatever it imagines, unlocked)

  style [1]:
```

Pass `--style` and it never asks. Pass `--yes` and it never asks anything at
all, which is what a scheduled run wants.

```bash
oneword styles                              # what is available
oneword rust --style noir                   # a series shot in black and white
oneword rust --style ./my-look.json         # your own, same keys as a preset
```

Eight presets ship: `documentary` `noir` `anime` `storybook` `16mm` `clinical`
`analog` `stopmotion`. Each names the **medium** first — photographic, cel
animation, watercolour — then lens, lighting, palette, tone, and what the look
must never contain.

Combining two looks is a written choice, not a flag. `16mm` wants warm fading
stock; `noir` wants no colour at all. Overriding one preset with the other just
gives you the second one, so `styles/16mm-noir.json` picks the fields one at a
time — the stock and grain from one, the light and palette from the other:

```bash
oneword rust --style styles/16mm-noir.json
```

Copy it as the starting point for your own.

**Style does not affect what a clip costs.** Billing follows resolution and
duration; the prompt text does not enter into it. Pick the look you want and
save money on `SEEDANCE_RESOLUTION` instead.

### A style is locked, not a dial

The argument this tool makes is that the look does not drift. A style that can
be changed casually destroys that argument, so a style is chosen once, written
into the bible, and injected byte-identically into every prompt of every
episode — exactly like a character's torn cuff.

Restyling an existing series is allowed and is a deliberate act:

```bash
oneword rust --bible out/rust/bible.json --style anime --reset-references
```

It rewrites the style block and **leaves the cast, the locations and the
episode beats untouched** — a change of medium does not change who anyone is.
The torn cuff is still torn when the series is redrawn as animation.

### Why it forces a re-base

Every reference still records the style it was shot in. In a new look, every
frame legitimately differs from the old one, so comparing across a restyle
would report a series that has fallen apart when all that happened is you
picked a preset. `oneword drift` refuses rather than producing that report:

```
the references were shot in noir but this bible is now anime. Every frame
differs by design, so a comparison would report drift that is not drift.
  Re-base them deliberately: oneword drift <dir> --reset-references
```

## Cross-episode drift

Everything above works inside one episode. This is the part that looks across
them, and the part a one-prompt pipeline cannot build: comparing episode 9 to
episode 1 needs a written-down reference, and a pipeline that keeps its facts
in a context window has nothing to point at.

```bash
oneword drift out/rust          # or it runs automatically after a multi-episode run
```

The first time a location or character appears and passes, that frame is
adopted as its reference still and written to `references/`. Every later
appearance, in every later episode and every later run, is compared **against
that same still** — never against the previous episode.

That distinction is the whole design. Comparing each episode to the last one is
the arrangement that guarantees slow failure: episode 2 drifts three percent
and passes, becomes the new reference, episode 3 drifts three percent from
*that*, and by episode 9 nothing resembles episode 1 while every check passed
along the way. So references are frozen once adopted. Re-basing exists, as
`--reset-references`, and it is recorded in the registry with the episode that
caused it.

### What it can and cannot decide

| subject | screened locally | why |
|---|---|---|
| location | yes | the frame is mostly the location, so a cheap fingerprint says something real |
| character | **no** | a person is a fraction of a frame they share with a set that legitimately changes; a whole-frame fingerprint would be measuring the room |

With no multimodal model configured, character drift is reported as
`NOT CHECKED` — never as a pass — and the series summary is `PARTIAL` rather
than `CONSISTENT`. A green tick nobody earned is worse than an honest blank.

It is also the only thing that can tell you whether the cast portraits worked,
so that is the reason to configure one. Where a character has a locked
portrait, the drift pass compares against **the portrait itself** rather than a
frame from whichever shot they happened to appear in first — the picture that
was actually sent to every later shot, asking the exact question that matters.
And a shot whose portraits the platform refused is marked as such, so a face
that drifted in that shot reads as a shot the mechanism never touched rather
than as a mechanism that failed.

The local screen is triage, not a verdict. Its thresholds were measured, not
chosen, and the measurement says the distributions overlap: about 80% of
comparisons land in a review band the screen refuses to decide alone, and get
handed to the multimodal comparison, which reads the written facts — the torn
cuff, the green handrail — instead of counting pixels.
[`docs/drift-calibration.md`](docs/drift-calibration.md) has the numbers and the
command that reproduces them.

### Why did *this* one drift

A distance says how far apart two frames are. It does not say what was
different about how the shot was made — and that answer is already on disk,
because every episode report records whether a shot continued the previous one,
whether the platform refused its first frame, and whether the cast portraits
were sent. Joining the two costs nothing:

```bash
python scripts/explain_drift.py out/rust
```

```
ep  shot  subject          verdict        comp    col    str  how it was shot
 1     2  Stairwell C      CONSISTENT    0.010  0.012  0.008  chained (from shot 1)
 2     2  Stairwell C      REVIEW        0.075  0.109  0.013  chained (from shot 1)  vs shot 1: 0.010
 2     4  Unit 704         DRIFTED       0.123  0.186  0.007  chain refused: InputImage…

By how the shot was made
  chained          n=2  min 0.010 · p50 0.043 · max 0.075
  chain refused    n=2  min 0.112 · p50 0.117 · max 0.123
```

This is how the tool found out that **a chained shot resembles the shot it
continued, not the series reference** — and inherits whatever that shot had
already lost. One chained shot in the first real run sits 0.010 from its
reference; another sits 0.075, because it faithfully continued a shot that had
drifted. So there are two numbers worth reading, not one:

| | what it answers |
|---|---|
| distance to the **reference** | has the series held? |
| distance to the **predecessor** | did this cut hold? |

Tight to the predecessor and far from the reference means the whole run moved
together. Regenerating one of those shots reshoots something that already
matches its neighbour exactly — what needs re-establishing is the episode. The
report says that in those words when it sees the pattern, instead of filing one
episode's drift as four unrelated broken shots.

The same run reports **which channel decided each row**. On real series footage
the rooms really are the same rooms, so the structural channel correctly barely
moves and colour carries the whole verdict — the composite is a one-channel
measurement wearing a two-channel label. It now says so rather than letting you
assume otherwise.

Measure the bands on your own footage once you have some:

```bash
python scripts/calibrate_drift.py --series out/rust
```

Where a population has too little footage to separate, it says that instead of
emitting a number.

### Not paying twice

A paid run that dies at shot 8 leaves seven finished clips on disk. By default
the next run uses them:

```bash
oneword rust --vendor seedance --out out-real     # picks up where it stopped
oneword rust --vendor seedance --fresh            # buy everything again
```

Every generated clip drops a small JSON beside it recording the prompt it was
made from, the vendor, and what it cost. A clip is reused only when the next
run would have asked for **exactly the same thing** — same prompt fingerprint,
same vendor, file still readable.

That one rule covers every case worth covering without special-casing any of
them. Edit the bible, change the style, switch model or resolution: the prompt
differs, the fingerprint misses, and the shot is made again. A resume that
silently kept a clip from a previous look would cost far more than re-buying
one.

The run says what it skipped and what that was worth:

```
  reused 7 shot(s) already on disk, saving ¥13.02
```

### What a clip costs

The budget cap is only worth having if the number behind it is real, so prices
are **not** shipped in the source. They are per-account, per-model, per-region
and they change; a number in the code is a number that is wrong for somebody,
and an upgrade would silently restore it over whatever they had corrected.

```bash
export ONEWORD_PRICE=1.86        # this run only
cp prices.example.json prices.json && $EDITOR prices.json   # or keep it
```

`prices.json` is model → resolution → duration → yuan:

```json
{ "doubao-seedance-2-0-mini-260615": { "480p": { "5": 1.86 } } }
```

An unpriced combination is refused before anything is submitted, and the error
prints the exact line to add.

## Rules about spending money

| | automatic retries | why |
|---|---|---|
| `POST` submit a task | **0** | a timed-out POST may already have created a billable task |
| `GET` poll / fetch | ≤2 | free and idempotent |

* Without `ENABLE_VIDEO_GENERATION=1` the paid vendor cannot even be constructed.
* Pricing is a whitelist you supply, not a formula and not a shipped guess.
  An unpriced (model, resolution, duration) combination is refused.
* `VIDEO_BUDGET_CNY` raises `BudgetExceeded` *before* submitting, not after.
* One run holds exactly one vendor; report validation rejects mixed-vendor runs.
* A paid vendor will not shoot the placeholder story without being told to.
  It prints what the run would cost and what the film would actually be, and
  waits for `yes`. Exit code 10 means you said no; nothing was generated and
  nothing was charged.

## Rules about telling the truth

These are enforced by `validate_episode_report()`, not by good intentions:

* A non-generative vendor can never carry a visual evidence label. No model
  looked at those pixels, so the report may not imply one did.
* A shot still failing after the repair cap reports `BLOCKERS REMAIN` with the
  shot numbers — never rounded up to `DELIVERED`.
* Only `severity: regenerate` spends money again, and only on that one shot.
* The report is rejected if it contains mixed vendors, more than two repair
  rounds, a whole-episode rerun, or any regeneration of a shot that was not a
  blocker in the previous round.

## Deeper checks

The three-frame audit here is deliberately shallow. For hand-maintained rule
packs, causal narrative auditing and a five-level evidence taxonomy, install the
verifier this project grew out of:

```bash
pip install "one-word-series[continuity]"
```

[Continuity-Agent](https://github.com/wargolet4ever/Continuity-Agent) — a
continuity checker for AI short films. It catches the errors you cannot see in
a single frame. Entirely optional; nothing here requires it.

## What has actually been run

Claims in this README are things the code does. This section is the narrower
set of things that have been done for real, with money, against the live API:

* **A paid Seedance run, end to end.** Prompts composed from the bible,
  submitted to Ark, polled, downloaded, audited and assembled into a finished
  episode. Not a mock.
* **The cost guard on the path it guards.** The price whitelist refused an
  unlisted combination, and the budget cap is checked before a submit, not
  after.
* **Failures that only a live API produces.** Ark answers an unopened or
  misspelled model id with a 404 whose body holds the real reason; the adapter
  now surfaces that instead of `HTTP Error 404`. Generation takes minutes per
  clip, so the run reports progress rather than sitting silent.

* **A cross-episode drift pass over real footage**, which falsified a guess
  written down in this repo. The calibration doc predicted that a locked series
  would vary *less* than the synthetic harness; two real episodes varied
  **more** — four of six same-room readings landed beyond the harness's worst
  same-room pair. It also showed structure barely moving while colour carried
  every verdict. Both are now reported rather than assumed, and the bands are
  split by population. [`docs/drift-calibration.md`](docs/drift-calibration.md)
  has the six readings.

Everything else — the repair loop under real failure, the character comparison —
is exercised by tests and by the offline vendor, which is not the same thing as
proven in production. Where that distinction matters it is stated in place.

## Not done yet

1. **Character drift without a model.** Locations are screened locally;
   characters need a multimodal key or they are honestly left unchecked. No run
   has yet had one, so nothing has checked a face in this repo.
2. **The one band, measured on real footage.** It still comes from the
   synthetic harness, which the first real run showed to be *looser* than
   reality rather than harsher — a room that never changed read 0.072 and
   0.123. `scripts/calibrate_drift.py --series` measures real clips; the
   numbers have not been moved yet because two episodes is not a distribution.
3. **A demo film in the repo.** Paid clips exist; none is committed here yet.
4. **Character portraits against the live API.** The request shape, the
   degradation ladder and the freezing rules are asserted by tests and driven
   through a fake opener. Whether Ark's moderation accepts an *adopted* frame
   from its own output — a face it generated — has not been paid for and
   found out yet. Expect the drop-to-text path to fire; it is built for that.

## Windows notes

Everything below degrades instead of failing, so a bare Windows box still
produces a film:

* **ffmpeg** comes from the `imageio-ffmpeg` dependency. You do not need to
  install it separately or put it on PATH.
* **espeak-ng** is not on Windows by default, so `--voice auto` ships the
  picture and subtitles without speech and says so. Install espeak-ng and put
  it on PATH for local speech, or use `--voice volc` for real voices.
* **Burned-in subtitles** need an ffmpeg built with libass. If yours is not,
  the `.mp4` and the `.srt` are still written — you just load the `.srt` in
  your player or your editor.

## Tests

```bash
python -m unittest discover -s tests -t .
```

227 tests, none of which touch a paid API. The Ark adapter is driven through a
fake opener, so the request shape, the no-retry-on-submit rule and the budget
cap are all asserted without spending anything.
