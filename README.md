# One Word Series · 一词成剧

**One word in, a whole series out — with the continuity written down.**

[中文](README.zh-CN.md) · MIT · Python ≥ 3.10

```bash
pip install -e .
oneword rust --episodes 2
```

That runs right now, with no API key and no spend, and hands you two finished
`.mp4` files with subtitles — and speech too, if this machine has a TTS engine.

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

```bash
oneword styles                              # what is available
oneword rust --style noir                   # a series shot in black and white
oneword rust --style ./my-look.json         # your own, same keys as a preset
```

Eight presets ship: `documentary` `noir` `anime` `storybook` `16mm` `clinical`
`analog` `stopmotion`. Each names the **medium** first — photographic, cel
animation, watercolour — then lens, lighting, palette, tone, and what the look
must never contain.

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

The local screen is triage, not a verdict. Its thresholds were measured, not
chosen, and the measurement says the distributions overlap: about 80% of
comparisons land in a review band the screen refuses to decide alone, and get
handed to the multimodal comparison, which reads the written facts — the torn
cuff, the green handrail — instead of counting pixels.
[`docs/drift-calibration.md`](docs/drift-calibration.md) has the numbers and the
command that reproduces them.

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

Everything else — the drift thresholds, the repair loop under real failure —
is exercised by tests and by the offline vendor, which is not the same thing as
proven in production. Where that distinction matters it is stated in place.

## Not done yet

1. **Character drift without a model.** Locations are screened locally;
   characters need a multimodal key or they are honestly left unchecked.
2. **Thresholds measured on real footage.** The drift bands are calibrated on a
   synthetic harness. Rerun `scripts/calibrate_drift.py` against real Seedance
   episodes and move the constants — now possible, not yet done.
3. **A demo film in the repo.** Paid clips exist; none is committed here yet.

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

124 tests, none of which touch a paid API. The Ark adapter is driven through a
fake opener, so the request shape, the no-retry-on-submit rule and the budget
cap are all asserted without spending anything.
