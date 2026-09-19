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

## Rules about spending money

| | automatic retries | why |
|---|---|---|
| `POST` submit a task | **0** | a timed-out POST may already have created a billable task |
| `GET` poll / fetch | ≤2 | free and idempotent |

* Without `ENABLE_VIDEO_GENERATION=1` the paid vendor cannot even be constructed.
* Pricing is a whitelist, not a formula. An unpriced (model, resolution,
  duration) combination is refused rather than guessed at.
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

## Not done yet

1. **First-frame chaining.** Every shot is independent text-to-video today.
   Consecutive shots in one location should chain through image-to-video using
   the previous shot's last frame — `vendors.py` already leaves the slot for it.
2. **Music and ambience.** There is only a dialogue track.
3. **Character drift without a model.** Locations are screened locally;
   characters need a multimodal key or they are honestly left unchecked.
4. **Thresholds measured on real footage.** The drift bands are calibrated on a
   synthetic harness. Rerun `scripts/calibrate_drift.py` against real Seedance
   episodes once they exist and move the constants.
5. **Evidence of a real paid run.** The adapter is written and mock-tested; it
   has not yet produced a paid clip.

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

49 tests, none of which touch a paid API. The Ark adapter is driven through a
fake opener, so the request shape, the no-retry-on-submit rule and the budget
cap are all asserted without spending anything.
