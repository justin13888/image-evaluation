"""Generate the per-implementation tunables overview (``docs/tunables.md``).

This is the single high-level view the issue-#4 owner asked for: every
implementation's knobs, swept axis, secondary variant series, and the knobs that
are intentionally not swept (with reasons) — all synthesized from the
``TUNABLE_SCHEMAS`` source of truth in ``bench_lib.models`` so it can never drift
silently from the code. ``./bench docs`` writes it; ``./bench docs --check``
fails CI if the committed file no longer matches the schemas.
"""

from typing import List

from bench_lib.models import (
    DECODER_SKIP_NOTES,
    IMPLEMENTATIONS,
    BenchmarkType,
    ImageFormat,
    Implementation,
    TunableSchema,
    schema_for,
)

# Stable presentation order (matches the ImageFormat enum).
_FORMAT_ORDER: List[ImageFormat] = list(ImageFormat)


def _base_encoders(fmt: ImageFormat) -> List[Implementation]:
    return [
        i
        for i in IMPLEMENTATIONS
        if i.format == fmt and i.type == BenchmarkType.ENCODE and not i.is_variant
    ]


def _decoders(fmt: ImageFormat) -> List[Implementation]:
    return [
        i
        for i in IMPLEMENTATIONS
        if i.format == fmt and i.type == BenchmarkType.DECODE and not i.is_variant
    ]


def _variant_tags(base_name: str, kind: str) -> List[str]:
    """The ``tag`` portion of the derived ``base@tag`` series of a given kind
    (``curated`` | ``oat``), in expansion order."""
    prefix = base_name + "@"
    return [
        i.name[len(prefix) :]
        for i in IMPLEMENTATIONS
        if i.variant_kind == kind and i.name.startswith(prefix)
    ]


def _axis_cell(schema: TunableSchema) -> str:
    """Human summary of the swept operating-point axis for one encoder."""
    if schema.quality_axis is None:
        return "— (single lossless point)" if schema.lossless else "— (no axis)"
    sweep = schema.quality_sweep
    span = f"{sweep[0]}→{sweep[-1]}" if len(sweep) > 1 else sweep[0]
    kind = "effort, lossless" if schema.lossless else "quality"
    return f"`{schema.quality_axis}` — {span} ({len(sweep)} pts, {kind})"


def _other_knobs_cell(schema: TunableSchema) -> str:
    """Non-axis knobs the binary reads, each annotated with its pinned preset
    value and a † marker when it is documented-skipped (never swept)."""
    parts = []
    for p in schema.params:
        if p.name == schema.quality_axis:
            continue
        pinned = schema.perf_preset.get(p.name, p.default)
        marker = "†" if p.skip_reason else ""
        parts.append(f"`{p.name}`={pinned}{marker}")
    return ", ".join(parts) if parts else "—"


def _md_escape(text: str) -> str:
    """Escape the pipe so a reason string can sit inside a Markdown table cell."""
    return text.replace("|", "\\|")


def _encoder_table(fmt: ImageFormat) -> List[str]:
    rows = [
        "| Encoder | Lang | Swept axis | Curated variants (default) | `--params all` adds | Other knobs read |",
        "|---|---|---|---|---|---|",
    ]
    for impl in _base_encoders(fmt):
        schema = schema_for(impl.name)
        curated = _variant_tags(impl.name, "curated")
        oat = _variant_tags(impl.name, "oat")
        rows.append(
            f"| `{impl.name}` | {impl.lang} | {_axis_cell(schema)} | "
            f"{', '.join('`%s`' % t for t in curated) or '—'} | "
            f"{', '.join('`%s`' % t for t in oat) or '—'} | "
            f"{_other_knobs_cell(schema)} |"
        )
    return rows


def _skipped_rows() -> List[str]:
    """Every intentionally-not-swept knob, across all encoders, with its reason —
    both wired-but-pinned knobs (`Tunable.skip_reason`) and library features never
    wired at all (`TunableSchema.skipped`)."""
    rows = []
    for impl in IMPLEMENTATIONS:
        if impl.type != BenchmarkType.ENCODE or impl.is_variant:
            continue
        schema = schema_for(impl.name)
        for p in schema.params:
            if p.skip_reason:
                rows.append((impl.name, p.name, "pinned (wired)", p.skip_reason))
        for name, reason in schema.skipped:
            rows.append((impl.name, name, "not wired", reason))
    return [
        f"| `{impl}` | `{_md_escape(knob)}` | {status} | {_md_escape(reason)} |"
        for impl, knob, status, reason in rows
    ]


def _decoder_rows() -> List[str]:
    rows = []
    for fmt in _FORMAT_ORDER:
        for impl in _decoders(fmt):
            notes = DECODER_SKIP_NOTES.get(impl.name, [])
            note_cell = (
                "; ".join(f"{_md_escape(k)} ({_md_escape(v)})" for k, v in notes)
                if notes
                else "—"
            )
            rows.append(f"| `{impl.name}` | {fmt.value} | {impl.lang} | {note_cell} |")
    return rows


def render_tunables_markdown() -> str:
    """Render the full overview as a Markdown string (deterministic — no
    timestamps — so ``--check`` is a stable equality test)."""
    out: List[str] = []
    out.append("# Implementation tunables & operating points\n")
    out.append(
        "> **Generated** by `./bench docs` from `TUNABLE_SCHEMAS` in "
        "`bench_lib/models.py`. Do not edit by hand — run `./bench docs` to "
        "regenerate, and `./bench docs --check` verifies it is in sync.\n"
    )
    out.append(
        "Every encoder is swept across one **quality/effort axis** (a "
        "rate-distortion curve for lossy encoders, a size-vs-effort curve for "
        "lossless ones). Secondary knobs are tested as distinct **variant series** "
        "(`impl@knob-value`, reusing the same binary): the **curated** set runs by "
        "default (`./bench run`, i.e. `--params variants`); `--params all` "
        "additionally sweeps every remaining enum/bool knob one-at-a-time; "
        "`--params axis` restores the legacy axis-only sweep. A knob marked † (and "
        "every row in *Intentionally not swept*) is deliberately held fixed — the "
        "reason is recorded so the decision lives in code (issue #4).\n"
    )

    out.append("## Encoders\n")
    for fmt in _FORMAT_ORDER:
        if not _base_encoders(fmt):
            continue
        out.append(f"### {fmt.value.upper()}\n")
        out.extend(_encoder_table(fmt))
        out.append("")

    out.append("## Intentionally not swept (documented as irrelevant / deferred)\n")
    out.append(
        "Knobs the implementation *reads* but is deliberately pinned (`pinned`), "
        "plus library features deliberately never wired (`not wired`).\n"
    )
    out.append("| Implementation | Knob(s) | Status | Why |")
    out.append("|---|---|---|---|")
    out.extend(_skipped_rows())
    out.append("")

    out.append("## Decoders (parameterless)\n")
    out.append(
        "Decoders take no tunables here — they consume the bitstream as-is and are "
        "scored against the format's golden (reference) decoder, so a bit-exact "
        "decoder scores ∞ and only an approximate path shows a finite PSNR. Library "
        "decode knobs that exist but are intentionally not exercised:\n"
    )
    out.append("| Decoder | Format | Lang | Library knobs not exercised |")
    out.append("|---|---|---|---|")
    out.extend(_decoder_rows())
    out.append("")

    return "\n".join(out) + "\n"
