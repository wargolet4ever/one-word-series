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

The rooms are synthetic. They stand in for real footage because there was no
corpus of model-generated series footage to calibrate against, and the guess
written here was that they are probably **harsher** than reality: a
stylistically locked series should vary its exposure and framing less than this
harness does.

So these numbers are a defensible starting point, not a final answer. The
method is the durable part; the two numbers are not.

## The first real footage said otherwise

Two Seedance episodes, 8 clips, two locations. Every reading below is a
*same-place* comparison — the same room against its own locked reference:

| subject | composite | colour | structure |
|---|---|---|---|
| Stairwell C, ep1 shot 2 | 0.010 | 0.012 | 0.008 |
| Stairwell C, ep2 shot 1 | 0.072 | 0.103 | 0.013 |
| Stairwell C, ep2 shot 2 | 0.075 | 0.109 | 0.013 |
| Unit 704, ep1 shot 4 | 0.112 | 0.168 | 0.007 |
| Unit 704, ep2 shot 3 | 0.112 | 0.169 | 0.006 |
| Unit 704, ep2 shot 4 | 0.123 | 0.186 | 0.007 |

Three things fall out of six rows.

**The guess above was backwards.** The harness's worst same-room pair was
0.0893. Four of these six real same-room readings are further apart than that,
and the flag bar at 0.09 — justified as "the harness never produced the same
room this far apart" — is cleared by real footage of a room that never changed.
A locked series varies *more* than the synthetic harness, not less.

**The composite is one channel.** Structure stayed inside 0.006–0.013 across
every row, contributing at most 0.005 to a composite judged against a 0.09 bar.
Colour ran 0.012 → 0.186. The fingerprint is behaving exactly as designed — the
rooms really are the same rooms, so the structural channel correctly says
nothing — but the consequence is that on series footage the verdict is
`0.65 × colour` and the blend is arithmetic. `metrics.tripped_by` now reports
which channel decided each row, so a report of all-colour verdicts says so on
its face instead of implying a two-channel measurement.

**The spread looked like a population split.** 0.010 sits an order of magnitude
below the rest, and the obvious hypothesis was that it is the one shot
continued from the previous shot's last frame while the rest are independent
text-to-video guesses — two populations measured with one ruler. That
hypothesis was wrong, and the next section is how.

## The matrix that killed the chain-state split

`scripts/explain_drift.py` joined the drift report to the episode reports, and
every appearance was then compared against every other, not only against its
locked reference:

```
Stairwell C            ep1s1    ep1s2    ep2s1    ep2s2
  ep1s1                    —    0.010    0.072    0.075
  ep1s2                0.010        —    0.068    0.072
  ep2s1                0.072    0.068        —    0.010
  ep2s2                0.075    0.072    0.010        —

Unit 704               ep1s3    ep1s4    ep2s3    ep2s4
  ep1s3                    —    0.112    0.112    0.123
  ep1s4                0.112        —    0.114    0.083
  ep2s3                0.112    0.114        —    0.116
  ep2s4                0.123    0.083    0.116        —
```

**Stairwell C is textbook cross-episode drift.** Episode 1 is internally tight
(0.010), episode 2 is internally tight (0.010), and the two sit 0.068–0.075
apart. Nothing is wrong with any individual shot; episode 2's stairwell is
simply a different-looking stairwell, consistently, and the tool caught exactly
the failure it was built for — at 0.072, just under the 0.09 flag bar.

**Unit 704 has no stable appearance at all.** Every pair is 0.083–0.123,
including two consecutive shots inside one episode. There is no cluster, so
"the reference is the outlier" is also wrong: if it were, the other three would
agree with each other. They do not.

The difference between the two rooms is chaining. Stairwell C's second shot in
each episode continued the first and reads 0.010 against it. Unit 704 never got
a successful chain — both its second shots had their first frame refused by
moderation — and reads 0.112–0.116 against its own predecessor.

**So chaining works, and it does not do what the band split assumed.** A
chained shot resembles *the shot it continued*, not the series reference, and
it faithfully inherits whatever offset that shot already carried. `ep2s2` is
chained and sits 0.010 from its predecessor and 0.075 from the reference,
because it continued a shot that had already drifted. Chain state therefore
does not describe the comparison a band is applied to, and `BANDS` is back to
one entry.

What the data does support is reporting **both** numbers. Tight to the
predecessor and far from the reference means the run moved as a unit, and
regenerating one of those shots would reshoot something that already matches
its neighbour exactly — the episode wants re-establishing against the
reference instead. `neighbour_distance` carries the second number, and the
report says so in those words when it sees the pattern.

## Measuring real footage

```bash
python scripts/calibrate_drift.py --series out/rust
```

Same comparison, real clips: the earlier shot's first frame as the reference,
the later shot's three sampled frames as candidates, best of the three — the
call `drift.audit_series` makes. It splits same-place pairs by how the later
shot was made and prints bands ready to paste into `BANDS`. If a population has
too little footage to separate, it says so instead of emitting a number.
