"""Video vendors.  One run owns exactly one vendor — never a mix.

`SeedanceVendor`  — Volcengine Ark (即梦 / Seedance).  Real, paid, opt-in.
`AnimaticVendor`  — local ffmpeg + PIL storyboard cards.  Real files, zero cost.

Both satisfy the same `FilmVendor` contract, so the audit/repair loop cannot
tell them apart and the free offline demo exercises the exact code path a paid
run takes.  Adding a model means writing one class with one `generate()`.

Retry policy is deliberately asymmetric:

    submit   0 automatic retries   — a timed-out POST may already have
                                     created a billable task
    poll     ≤2 automatic retries  — GET is free and idempotent
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import chain
from .contracts import BlockerFinding, GeneratedClip, OneWordError

ARK_DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"
ARK_DEFAULT_MODEL = "doubao-seedance-1-0-lite-t2v-250428"
POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 600
POLL_RETRIES = 2
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Deliberately empty. Prices are per-account, per-model, per-region and they
# change; a number shipped in source is a number that is wrong for somebody and
# that an upgrade silently restores over whatever they corrected. So the table
# lives outside the code — in prices.json or ONEWORD_PRICE — and an unpriced
# combination is refused rather than guessed at.
PRICE_CNY: dict[tuple[str, str, int], float] = {}

PRICE_FILE_NAME = "prices.json"


class VendorError(OneWordError):
    pass


class BudgetExceeded(VendorError):
    pass


class ArkHTTPError(VendorError):
    """An HTTP failure with the platform's own explanation attached.

    Ark puts the useful part in the response body — "model not found", "no
    access", "quota exhausted" — and a bare `HTTPError: 404` throws that away,
    leaving you to guess. It keeps `.code` so the retry classifier still works
    on it unchanged.
    """

    def __init__(self, code: int, message: str, url: str, model: str = "") -> None:
        detail = f"Ark returned HTTP {code}"
        if model:
            detail += f" for model {model!r}"
        detail += f": {message}" if message else " with no explanation in the body"
        if code == 404:
            detail += (
                "\n  A 404 here almost always means the model id is wrong or not "
                "activated on this account, not that the URL is wrong.\n"
                "  Check 方舟控制台 → 模型广场: is the model opened, and does "
                "SEEDANCE_MODEL match its id exactly (including the date suffix)?"
            )
        self.code = code
        self.url = url
        super().__init__(detail)


class ArkUnreachable(VendorError):
    """The request never got an HTTP response — the network stopped it first.

    This earns its own type because after a failed submit there is exactly one
    question worth answering: does a paid task now exist? A connection that was
    refused or whose host would not resolve never delivered the request, so the
    answer is a flat no. A connection that timed out mid-flight may have. The
    message says which, rather than leaving you to read forty lines of urllib
    internals and guess — which is what this used to be.

    It stays retryable, so the poll loop treats a dropped connection exactly as
    it did before. Only submit refuses to retry, and that rule is unchanged.
    """

    def __init__(self, url: str, reason: str, *, delivered: bool) -> None:
        host = urllib.parse.urlsplit(url).netloc or url
        detail = f"could not reach {host}: {reason}"
        detail += (
            "\n  The request may have been delivered, so a paid task MAY exist. "
            "Check 方舟控制台 → 任务管理 before rerunning."
            if delivered
            else "\n  The request was never delivered, so no task was created "
            "and nothing was charged."
        )
        # Same provenance habit as the price error: a wrong value is only
        # fixable once you know whether you set it or a default set it.
        base = os.getenv("ARK_BASE_URL")
        detail += (
            f"\n  ARK_BASE_URL={base}   (set)"
            if base
            else f"\n  ARK_BASE_URL is unset, so this is the built-in {ARK_DEFAULT_BASE}"
        )
        proxies = [
            f"{name}={os.environ[name]}"
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY")
            if os.environ.get(name)
        ]
        if proxies:
            detail += (
                "\n  A proxy is configured and urllib goes through it: "
                + " · ".join(proxies)
                + "\n  If that proxy is not running, this is the failure you get."
            )
        self.delivered = delivered
        self.url = url
        super().__init__(detail)


def _error_message(exc: urllib.error.HTTPError) -> str:
    """Pull the platform's explanation out of an error response body."""

    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — the body is a bonus, never required
        return ""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw.strip()[:400]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        parts = [str(error.get(key, "")).strip() for key in ("code", "message")]
        return " · ".join(part for part in parts if part)[:400]
    return raw.strip()[:400]


def _root_cause(exc: BaseException) -> BaseException:
    """The OS error underneath urllib's wrapper, which is the informative one."""

    reason = getattr(exc, "reason", None)
    return reason if isinstance(reason, BaseException) else exc


def _network_reason(exc: BaseException) -> str:
    """A one-line cause, in the OS's own words."""

    cause = _root_cause(exc)
    text = str(cause).strip()
    return f"{type(cause).__name__}: {text}" if text else type(cause).__name__


def _may_have_been_delivered(exc: BaseException) -> bool:
    """Could this failure have left a paid task on the platform?

    Only two cases prove it could not: a connection the far end actively
    refused, and a hostname that never resolved. In both the request was never
    put on the wire. Everything else — a timeout above all, which can fire
    after the platform has accepted the bytes — is treated as "maybe", because
    the expensive mistake here is telling someone nothing was charged when
    something was. Erring towards "go and look" costs a glance at the console;
    erring the other way costs a silent duplicate charge on the rerun.
    """

    cause = _root_cause(exc)
    if isinstance(cause, ConnectionRefusedError):
        return False
    if isinstance(cause, socket.gaierror):
        return False
    return True


# A platform can refuse the first frame itself — most often because the frame
# shows a photorealistic person, which moderation reads as a real photograph.
# The refusal is about the image, not the prompt, so the shot is still makeable.
INPUT_IMAGE_REFUSALS = (
    "inputimagesensitive",
    "input image",
    "image content",
    "may contain real person",
    # Structural rather than moral: Ark will not take a first frame and
    # reference images in one request. `generate` now chooses between them
    # up front so this should never fire, and it is listed anyway — a rule
    # about which images may travel together is exactly the kind of thing a
    # platform changes, and finding out should cost a degraded shot rather
    # than the rest of a paid episode.
    "cannot be mixed with reference",
)


def refused_the_input_image(exc: Exception) -> bool:
    if getattr(exc, "code", None) != 400:
        return False
    message = str(exc).lower()
    return any(marker in message for marker in INPUT_IMAGE_REFUSALS)


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise VendorError("ffmpeg unavailable; run `python diagnose.py`") from exc


# ──────────────────────────────────────────────────────────────────────
# Offline animatic vendor
# ──────────────────────────────────────────────────────────────────────

_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
    return ImageFont.load_default()


class AnimaticVendor:
    """Renders a real, watchable storyboard card per shot.  No API, no cost.

    It is honest about what it is: the report records the provider name, and
    nothing downstream claims a generative model produced these pixels.  Its
    only job is to prove the loop — plan, generate, audit, repair, assemble —
    end to end before a single yuan is spent.
    """

    name = "local-animatic"
    generative = False
    speaks = False
    accepts_first_frame = False
    accepts_reference_images = False

    def __init__(self, *, width: int = 1280, height: int = 720, fps: int = 24) -> None:
        self.ffmpeg = ffmpeg_exe()
        self.width = width
        self.height = height
        self.fps = fps

    def _card(self, shot: dict[str, Any], attempt: int, target: Path) -> Path:
        from PIL import Image, ImageDraw

        bg = (18, 22, 28)
        image = Image.new("RGB", (self.width, self.height), bg)
        draw = ImageDraw.Draw(image)

        accent = (196, 122, 74) if shot.get("identity_critical") else (74, 122, 150)
        draw.rectangle([0, 0, 10, self.height], fill=accent)

        head = _font(30)
        body = _font(40)
        small = _font(24)

        draw.text((60, 54), f"SHOT {shot['shot_id']} · {shot.get('beat', '').upper()}", font=head, fill=(150, 163, 176))
        draw.text((60, 96), shot.get("camera", ""), font=small, fill=(110, 122, 136))

        y = 190
        for line in textwrap.wrap(shot.get("action", ""), width=34)[:5]:
            draw.text((60, y), line, font=body, fill=(232, 236, 240))
            y += 56

        line_text = (shot.get("line") or "").strip()
        if line_text:
            y = self.height - 190
            draw.text((60, y - 40), "—", font=small, fill=(110, 122, 136))
            for line in textwrap.wrap(f"「{line_text}」", width=40)[:3]:
                draw.text((60, y), line, font=small, fill=accent)
                y += 34

        footer = f"{shot.get('location_name', '')}  ·  take {attempt}"
        draw.text((60, self.height - 60), footer, font=small, fill=(92, 103, 115))

        png = target.with_suffix(".png")
        image.save(png)
        return png

    def generate(
        self,
        shot: dict[str, Any],
        prompt: str,
        attempt: int,
        target: Path,
    ) -> GeneratedClip:
        target.parent.mkdir(parents=True, exist_ok=True)
        png = self._card(shot, attempt, target)
        duration = float(shot.get("duration_sec") or 5)
        # A slow push keeps the cut feeling like a shot rather than a slideshow.
        zoom = "zoompan=z='min(zoom+0.0008,1.10)':d={frames}:s={w}x{h}:fps={fps}".format(
            frames=int(duration * self.fps), w=self.width, h=self.height, fps=self.fps
        )
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-i", str(png),
            "-vf", f"{zoom},fade=t=in:st=0:d=0.4,fade=t=out:st={max(duration - 0.4, 0):.2f}:d=0.4,format=yuv420p",
            "-t", f"{duration:.2f}", "-r", str(self.fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-y", str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        png.unlink(missing_ok=True)
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()
            raise VendorError(f"animatic render failed: {detail[:240]}")
        return GeneratedClip(
            shot_id=str(shot["shot_id"]),
            attempt=attempt,
            provider=self.name,
            path=target,
            prompt=prompt,
        )


# ──────────────────────────────────────────────────────────────────────
# Seedance (Volcengine Ark)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ArkConfig:
    api_key: str
    base_url: str = ARK_DEFAULT_BASE
    model: str = ARK_DEFAULT_MODEL
    resolution: str = "720p"
    ratio: str = "16:9"
    duration: int = 5
    watermark: bool = False
    generate_audio: bool = False
    budget_cny: float = 30.0

    @classmethod
    def from_env(cls) -> "ArkConfig":
        key = os.getenv("ARK_API_KEY") or os.getenv("SEEDANCE_API_KEY") or ""
        if not key:
            raise VendorError("ARK_API_KEY is not set; refusing to build a paid vendor")
        if os.getenv("ENABLE_VIDEO_GENERATION") != "1":
            raise VendorError("ENABLE_VIDEO_GENERATION=1 is required before any paid generation")
        return cls(
            api_key=key,
            base_url=os.getenv("ARK_BASE_URL", ARK_DEFAULT_BASE).rstrip("/"),
            model=os.getenv("SEEDANCE_MODEL", ARK_DEFAULT_MODEL),
            resolution=os.getenv("SEEDANCE_RESOLUTION", "720p"),
            ratio=os.getenv("SEEDANCE_RATIO", "16:9"),
            duration=int(os.getenv("SEEDANCE_DURATION", "5")),
            watermark=os.getenv("SEEDANCE_WATERMARK", "0") == "1",
            generate_audio=os.getenv("SEEDANCE_AUDIO", "0") == "1",
            budget_cny=float(os.getenv("VIDEO_BUDGET_CNY", "30")),
        )


def price_file() -> Path:
    """Where the price table lives.  ONEWORD_PRICES overrides the default."""

    override = os.getenv("ONEWORD_PRICES")
    return Path(override) if override else Path.cwd() / PRICE_FILE_NAME


def load_prices() -> dict[tuple[str, str, int], float]:
    """Built-ins, then prices.json, then ONEWORD_PRICE for the current run.

    Shape of the file — model, resolution, duration in seconds:

        {"doubao-seedance-2-0-mini-260615": {"480p": {"5": 1.86}}}
    """

    table = dict(PRICE_CNY)
    path = price_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise VendorError(f"{path} is not valid JSON: {exc}") from exc
        for model, by_resolution in (data or {}).items():
            for resolution, by_duration in (by_resolution or {}).items():
                for duration, price in (by_duration or {}).items():
                    try:
                        table[(str(model), str(resolution), int(duration))] = float(price)
                    except (TypeError, ValueError) as exc:
                        raise VendorError(
                            f"{path}: bad price for {model}/{resolution}/{duration}"
                        ) from exc
    return table


def estimated_cost_cny(config: ArkConfig) -> float:
    key = (config.model, config.resolution, int(config.duration))

    # A single price for this exact run, for when you just want to go.
    direct = os.getenv("ONEWORD_PRICE")
    if direct:
        try:
            return float(direct)
        except ValueError as exc:
            raise VendorError(f"ONEWORD_PRICE is not a number: {direct!r}") from exc

    table = load_prices()
    if key not in table:
        model, resolution, duration = key
        # Naming the combination is not enough: the first question anyone asks
        # is "why THAT model?", because the video model is a different setting
        # from the writing model and an unset one falls back to a built-in
        # default that is probably not what you just configured. Say where each
        # half came from, so a wrong model gets fixed instead of priced.
        provenance = []
        for name, value, fallback in (
            ("SEEDANCE_MODEL", model, ARK_DEFAULT_MODEL),
            ("SEEDANCE_RESOLUTION", resolution, "720p"),
            ("SEEDANCE_DURATION", str(duration), "5"),
        ):
            source = "set" if os.getenv(name) else f"UNSET — built-in default {fallback}"
            provenance.append(f"    {name}={value}   ({source})")
        raise VendorError(
            f"no verified price for {model} at {resolution}/{duration}s.\n"
            "  That combination came from:\n"
            + "\n".join(provenance)
            + "\n  If it is not the one you meant, set those first — the writing model "
            "(LLM_MODEL)\n  is a separate setting and does not change which video model "
            "is used.\n"
            "  If it is right, look the price up on the platform's pricing page — a "
            "guessed\n  number makes the budget cap meaningless — then either:\n"
            f"    set ONEWORD_PRICE=<yuan per clip>   (this run only)\n"
            f"    or put it in {price_file()}:\n"
            f'      {{"{model}": {{"{resolution}": {{"{duration}": 1.86}}}}}}'
        )
    return table[key]


class SeedanceVendor:
    """Submit → poll → download, against the Ark native video API."""

    name = "seedance-ark"
    generative = True
    # Image-to-video: the previous shot's last frame can be handed in as this
    # shot's first frame, which is what makes a cut continuous rather than a
    # second independent guess at the same room.
    accepts_first_frame = True
    # Character portraits, sent with every shot that person appears in. Words
    # describe a type; only a picture fixes a face.
    accepts_reference_images = True

    def __init__(
        self,
        config: ArkConfig | None = None,
        *,
        opener=None,
        on_progress=None,
    ) -> None:
        self.config = config or ArkConfig.from_env()
        self.unit_cost = estimated_cost_cny(self.config)
        self.spent_cny = 0.0
        self.ffmpeg = ffmpeg_exe()
        self._opener = opener or urllib.request.urlopen
        # Generating one clip takes minutes, and a run that prints nothing for
        # minutes is indistinguishable from a hung one. The vendor reports what
        # it is waiting on; the caller decides how to show it.
        self._on_progress = on_progress
        # Whether this vendor can perform dialogue itself, which decides
        # whether the pipeline writes lines into the prompt at all.
        self.speaks = bool(self.config.generate_audio)
        self.events: list[dict[str, Any]] = []

    def _progress(self, **fields: Any) -> None:
        if self._on_progress:
            self._on_progress(fields)

    # ---- HTTP ------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self._opener(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ArkHTTPError(
                exc.code,
                _error_message(exc),
                f"{self.config.base_url}{path}",
                model=self.config.model,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # No HTTP response came back at all. Whether a paid task exists
            # depends entirely on how far the request got, so work that out
            # here, where the OS error is still in hand, rather than making
            # the caller infer it from a traceback.
            raise ArkUnreachable(
                f"{self.config.base_url}{path}",
                _network_reason(exc),
                delivered=_may_have_been_delivered(exc),
            ) from exc

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code in RETRYABLE_STATUS
        return isinstance(
            exc, (ArkUnreachable, urllib.error.URLError, TimeoutError, ConnectionError)
        )

    # ---- the three stages ------------------------------------------

    def submit(
        self,
        prompt: str,
        first_frame: Path | None = None,
        reference_images: list[Path] | None = None,
    ) -> str:
        if self.spent_cny + self.unit_cost > self.config.budget_cny:
            raise BudgetExceeded(
                f"budget cap ¥{self.config.budget_cny:.2f} reached "
                f"(spent ¥{self.spent_cny:.2f}, next clip ¥{self.unit_cost:.2f})"
            )
        suffix = (
            f" --resolution {self.config.resolution}"
            f" --ratio {self.config.ratio}"
            f" --duration {int(self.config.duration)}"
            f" --watermark {'true' if self.config.watermark else 'false'}"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt.strip() + suffix}]
        if first_frame is not None:
            # Sent inline rather than as a link: the frame lives on this
            # machine and Ark cannot reach a local path.
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": chain.data_url(Path(first_frame))},
                    "role": "first_frame",
                }
            )
        for portrait in reference_images or []:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": chain.data_url(Path(portrait))},
                    "role": "reference_image",
                }
            )
        payload = {"model": self.config.model, "content": content}
        if self.config.generate_audio:
            payload["generate_audio"] = True
        # No retry here, on purpose: a retried POST can create a second paid task.
        body = self._request("POST", "/contents/generations/tasks", payload)
        task_id = body.get("id") or body.get("task_id")
        if not task_id:
            raise VendorError(f"Ark accepted the request but returned no task id: {str(body)[:200]}")
        self.spent_cny += self.unit_cost
        return str(task_id)

    def poll(self, task_id: str, *, shot_id: str = "") -> dict[str, Any]:
        started = time.time()
        deadline = started + POLL_TIMEOUT_S
        failures = 0
        while time.time() < deadline:
            try:
                body = self._request("GET", f"/contents/generations/tasks/{task_id}")
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                if not self._retryable(exc) or failures > POLL_RETRIES:
                    raise VendorError(f"polling {task_id} failed: {type(exc).__name__}") from exc
                self._progress(
                    shot_id=shot_id, task_id=task_id, status="retrying",
                    elapsed=time.time() - started, attempt_failures=failures,
                )
                time.sleep(POLL_INTERVAL_S)
                continue

            status = (body.get("status") or "").lower()
            if status == "succeeded":
                self._progress(
                    shot_id=shot_id, task_id=task_id, status="succeeded",
                    elapsed=time.time() - started, done=True,
                )
                return body
            if status in {"failed", "canceled"}:
                error = body.get("error") or {}
                raise VendorError(
                    f"Ark task {task_id} {status}: {error.get('code', '')} {error.get('message', '')}".strip()
                )
            self._progress(
                shot_id=shot_id, task_id=task_id, status=status or "pending",
                elapsed=time.time() - started,
            )
            time.sleep(POLL_INTERVAL_S)
        raise VendorError(f"Ark task {task_id} did not finish within {POLL_TIMEOUT_S}s")

    def download(self, url: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, method="GET")
        with self._opener(request, timeout=300) as response, target.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if not target.is_file() or target.stat().st_size == 0:
            raise VendorError(f"downloaded clip is empty: {target.name}")
        return target

    # ---- FilmVendor ------------------------------------------------

    def generate(
        self,
        shot: dict[str, Any],
        prompt: str,
        attempt: int,
        target: Path,
    ) -> GeneratedClip:
        started = time.time()
        shot_id = str(shot["shot_id"])
        first_frame = shot.get("first_frame")
        self._progress(
            shot_id=shot_id,
            status="submitting" + (" (continuing)" if first_frame else ""),
            elapsed=0.0,
        )
        portraits = [Path(p) for p in (shot.get("reference_images") or [])]
        frame = Path(first_frame) if first_frame else None

        # Ark refuses a request carrying both: "first/last frame content cannot
        # be mixed with reference media content". So this is a choice, not a
        # stack — and the chain wins wherever it exists, because the frame it
        # hands over ALREADY contains the character as the previous shot
        # established them. Chaining carries the room *and* the face; a
        # portrait carries only the face. Portraits are therefore for the shots
        # a chain cannot reach — the first shot of a location, the first of an
        # episode — which is where identity has nothing else holding it.
        superseded: str | None = None
        if frame is not None and portraits:
            superseded = (
                f"superseded by the first frame, which already carries the face; "
                f"Ark refuses a first frame and reference images in one request"
            )
            portraits = []

        # Input images are moderated, and a photorealistic face is exactly what
        # gets refused. Both the first frame and the portraits are improvements,
        # not requirements, so a refusal steps down one rung rather than ending
        # a run that has already been paid for. A refused submit creates no
        # task, so each attempt on this ladder costs nothing.
        # One input-image rung at most, now that the two are exclusive.
        ladder = []
        if frame or portraits:
            ladder.append((frame, portraits))
        ladder.append((None, []))

        chain_dropped: str | None = None
        references_dropped: str | None = superseded
        task_id = None
        for index, (try_frame, try_portraits) in enumerate(ladder):
            try:
                task_id = self.submit(prompt, try_frame, try_portraits)
            except ArkHTTPError as exc:
                if not refused_the_input_image(exc) or index == len(ladder) - 1:
                    raise
                # 300, not 200: the platform's reason runs past 200 characters
                # and a note that stops mid-sentence is not a reason.
                reason = str(exc).split("\n")[0][:300]
                if try_frame is not None:
                    chain_dropped = reason
                if try_portraits and not ladder[index + 1][1]:
                    references_dropped = reason
                self._progress(
                    shot_id=shot_id,
                    status="input image refused — retrying with less",
                    elapsed=time.time() - started,
                )
                continue
            if try_frame is None and frame is not None and chain_dropped is None:
                chain_dropped = "first frame was not used"
            if not try_portraits and portraits and references_dropped is None:
                references_dropped = "portraits were not used"
            break
        body = self.poll(task_id, shot_id=shot_id)
        content = body.get("content") or {}
        url = content.get("video_url") or content.get("url")
        if not url:
            raise VendorError(f"Ark task {task_id} succeeded with no video_url")
        self._progress(
            shot_id=shot_id, task_id=task_id, status="downloading",
            elapsed=time.time() - started,
        )
        self.download(url, target)
        self._progress(
            shot_id=shot_id, task_id=task_id, status="saved", done=True,
            elapsed=time.time() - started, cost_cny=self.unit_cost,
            spent_cny=self.spent_cny,
        )
        self.events.append(
            {
                "shot_id": str(shot["shot_id"]),
                "attempt": attempt,
                "task_id": task_id,
                "duration_ms": int((time.time() - started) * 1000),
                "cost_cny": self.unit_cost,
            }
        )
        return GeneratedClip(
            shot_id=str(shot["shot_id"]),
            attempt=attempt,
            provider=self.name,
            path=target,
            prompt=prompt,
            chain_dropped=chain_dropped,
            references_dropped=references_dropped,
        )


def build_vendor(kind: str, *, on_progress=None) -> Any:
    kind = (kind or "animatic").lower()
    if kind in {"animatic", "offline", "local"}:
        return AnimaticVendor()
    if kind in {"seedance", "ark", "jimeng"}:
        return SeedanceVendor(on_progress=on_progress)
    raise VendorError(f"unknown vendor '{kind}'; use animatic or seedance")
