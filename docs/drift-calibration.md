# Where the drift thresholds come from

`oneword/drift.py` has two numbers in it. This is the measurement they came
from, and the command that redoes it:

```bash
python scripts/calibrate_drift.py
```

## The question

Not "are these two frames different" — every two frames are. The question is
**is this difference bigger than the difference a legitimate second shot of the
same place already produces?**

So the harness builds a case with a known answer. A *room* is a procedural
image with its own palette and architecture. A *shot* of that room varies what
a real second setup varies — camera crop between 70% and 100% of frame,
exposure ±25%, saturation ±25%, grain, and a subject standing somewhere
different each time — while keeping the room. Then:

* **same room** = two shots of one room. Must not read as drift.
* **different room** = shots of two different rooms. Should read as drift.

6 rooms × 8 shots each → 168 same-room pairs and 960 different-room pairs.

## The measurement

| | n | min | p50 | p95 | max |
|---|---|---|---|---|---|
| same room | 168 | 0.0151 | 0.0386 | 0.0610 | 0.0893 |
| different room | 960 | 0.0316 | 0.0747 | 0.1112 | 0.1324 |

The medians are nearly 2× apart, so the fingerprint carries real signal. **But
the distributions overlap**: the worst same-room pair (0.0893) is further apart
than most different-room pairs, and the closest different-room pair (0.0316) is
nearer than half of all same-room pairs.

There is no cut that separates them. Any single threshold would be a number
chosen to look decisive.

## What that implies, and what was done about it

The two errors do not cost the same. Calling real drift "consistent" ships a
broken series and nobody finds out until episode 9. Calling a fine shot
"review" costs one model call. So the bands are set by that asymmetry rather
than by a split point:

```python
SAME_PLACE_MAX      = 0.03   # under the closest different-room pair ever seen
DIFFERENT_PLACE_MIN = 0.09   # over the furthest same-room pair ever seen
```

* `≤ 0.03` → **CONSISTENT**. The harness never produced a genuinely different
  room this close, so a pass here is one the data supports.
* `≥ 0.09` → **DRIFTED**. The harness never produced the same room this far
  apart, so a flag here is one the data supports.
* between → **REVIEW**, and that band covers about 80% of all pairs.

That 80% is the honest headline. **The pixel screen is triage, not a verdict.**
Its job is to answer the extremes for free and hand everything else to the
multimodal comparison, which reads the actual written facts — the torn cuff,
the green handrail — instead of counting pixels.

## The exposure fix

An early version compared the raw colour grid and scored a relight almost as
far as a new room. Subtracting each frame's own mean before comparing
(`metrics._exposure_normalised`) fixed that:

| | same-room p95 | same-room max | closest different-room |
|---|---|---|---|
| raw grid | 0.0814 | 0.1072 | 0.0313 |
| exposure-normalised | 0.0610 | 0.0893 | 0.0316 |

Same-room distances tightened by about 25% while the closest different-room
pair barely moved — the direction that matters, because it lets the flag bar
come down without new false alarms.

## What this calibration is not

The rooms are synthetic. They stand in for real footage because there is no
corpus of model-generated series footage to calibrate against yet, and they are
probably **harsher** than reality: a stylistically locked series varies its
exposure and framing less than this harness does.

So these numbers are a defensible starting point, not a final answer. Once
there are real Seedance episodes on disk, rerun the same comparison against
them and move the constants. The method is the durable part; the two numbers
are not.
