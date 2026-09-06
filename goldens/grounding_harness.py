"""Loader and stand-in model for the grounding corpus.

A case is a directory under ``grounding/``:

    grounding/<NNNN_name>/
        sources/*.md    -- source items, one per file, id and title derived as elsewhere
        case.json       -- the claims, what each should be judged, and a `why`

**Why this corpus needs a model at all, when the rest of the golden set does not.**
``FakeGroundingValidator`` checks that a claim's text *is* its span, which is the strict
version of invariant 3 and the right thing for the dedup/script corpus to run against —
but it never assembles a prompt, so it cannot say anything about the question this corpus
exists for: *what does the gate get to see when it judges?* That question is motet#45, and
the only way to ask it is to run the real :class:`ClaudeGroundingValidator` and answer it
with something deterministic.

:class:`NumericJudge` is that something. It is a fake in the sense every other fake here
is one — it implements the contract honestly with a trivial rule standing in for a model —
and the rule it implements is the *first* failure the grounding prompt names: a number in
the spoken text that the source does not state. Crucially it reads that source out of the
prompt it was handed and out of nothing else, so a case's verdict is a statement about the
evidence the validator assembled rather than about anything the harness knows.

No vendor is called, here or anywhere in the golden set (invariant 7).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from motet_inference import SourceItem, first_sentence
from motet_inference.llm import LlmRequest, LlmResponse, Usage

CASES_DIR = Path(__file__).resolve().parent / "grounding"


@dataclass(frozen=True)
class ExpectedClaim:
    """One claim a case puts in front of the gate, and the verdict it must come back with."""

    spoken: str
    cited: str
    source: str | None
    supported: bool


@dataclass(frozen=True)
class GroundingCase:
    name: str
    why: str
    sources: tuple[SourceItem, ...]
    claims: tuple[ExpectedClaim, ...]
    #: Substrings that must appear in what the gate was shown, and substrings that must
    #: not. The second is the load-bearing one: it is how a case says "the widening stops
    #: here", which no verdict on its own can express.
    evidence_contains: tuple[str, ...]
    evidence_excludes: tuple[str, ...]
    #: How many distinct SOURCE blocks the prompt should carry, when the case pins it.
    source_blocks: int | None

    def __str__(self) -> str:
        return self.name


def load_case(directory: Path) -> GroundingCase:
    spec = json.loads((directory / "case.json").read_text())
    source_paths = sorted((directory / "sources").glob("*.md"))
    if not source_paths:
        raise ValueError(f"grounding case {directory.name} has no sources")

    sources = tuple(
        SourceItem(id=path.stem, title=first_sentence(text), text=text)
        for path, text in ((p, p.read_text()) for p in source_paths)
    )
    claims = []
    for raw in spec["claims"]:
        cited = raw["cited"]
        if "\n" in cited:
            # NumericJudge reads the prompt a line at a time, so a citation that wrapped
            # would be mis-parsed into the next field rather than judged. Refused at load
            # time, where the message says so, instead of surfacing as a wrong verdict.
            raise ValueError(f"grounding case {directory.name}: a cited span must be one line")
        claims.append(
            ExpectedClaim(
                spoken=raw["spoken"],
                cited=cited,
                source=raw.get("source"),
                supported=_expectation(raw["expected"], directory.name),
            )
        )
    return GroundingCase(
        name=directory.name,
        why=spec["why"],
        sources=sources,
        claims=tuple(claims),
        evidence_contains=tuple(spec.get("evidence_contains", ())),
        evidence_excludes=tuple(spec.get("evidence_excludes", ())),
        source_blocks=spec.get("source_blocks"),
    )


def _expectation(value: str, case: str) -> bool:
    if value not in ("supported", "unsupported"):
        raise ValueError(f"grounding case {case}: expected must be supported or unsupported")
    return value == "supported"


def load_cases() -> Iterator[GroundingCase]:
    for directory in sorted(CASES_DIR.iterdir()):
        if directory.is_dir():
            yield load_case(directory)


#: A number as a newsletter writes one: digits, with grouping and decimal separators, and
#: whatever trailing punctuation the sentence happened to end with left off.
_NUMBER = re.compile(r"\d[\d,.]*")


def _numbers(text: str) -> set[str]:
    return {match.group().rstrip(".,").replace(",", "") for match in _NUMBER.finditer(text)}


@dataclass
class _PromptClaim:
    index: int
    spoken: str = ""
    source: int | None = None


class NumericJudge:
    """A stand-in model that judges one thing, using only what the prompt showed it.

    Supported iff every number in a claim's SPOKEN text appears somewhere in the SOURCE
    block that claim names. That is the first entry in the grounding prompt's own list of
    what is not supported, it is the failure the staging false positive was misread as,
    and it is deterministic — which is the whole reason this corpus can gate CI.

    **A claim whose SOURCE block cannot be found is unsupported.** Fail closed, the same
    rule the validator itself follows for a claim it got no verdict for: a judge that
    could not see the evidence has not checked anything, and "unchecked" must never come
    back as "supported".
    """

    #: What this judge says when it has read the evidence and refused the claim on it. A
    #: case asserts on this exact string, so that a refusal it expected cannot be satisfied
    #: by the gate failing closed for some *other* reason — a verdict that never arrived, a
    #: chunk that ran out of budget, or :data:`NO_BLOCK` below. Every one of those is
    #: correct behaviour, and none of them is what a refusing case is testing.
    REFUSED = "a number in the spoken text is not stated by the source"

    #: What it says when it could not find the block the claim named. Distinct from
    #: :data:`REFUSED` deliberately: they are the difference between "the evidence does not
    #: support this" and "I never saw the evidence", and a corpus that could not tell them
    #: apart would score every refusing case green against a prompt nobody could read.
    NO_BLOCK = "this claim names no source block"

    def __init__(self) -> None:
        #: Every user prompt this judge was sent, in order — what a case asserts against.
        self.prompts: list[str] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        prompt = request.messages[-1].text
        self.prompts.append(prompt)
        blocks, claims = parse_grounding_prompt(prompt)
        verdicts = []
        for claim in claims:
            if claim.source not in blocks:
                verdicts.append({"index": claim.index, "supported": False, "reason": self.NO_BLOCK})
                continue
            supported = _numbers(claim.spoken) <= _numbers(blocks[claim.source])
            verdicts.append(
                {
                    "index": claim.index,
                    "supported": supported,
                    "reason": "" if supported else self.REFUSED,
                }
            )
        return LlmResponse(
            text=json.dumps({"verdicts": verdicts}),
            model=request.model,
            usage=Usage(),
            reasoning_applied=True,
            finish_reason="stop",
        )


_BEGIN_SOURCE = re.compile(r"^BEGIN SOURCE (\d+) ([0-9a-f]+)$")
_CLAIM_HEADING = re.compile(r"^CLAIM (\d+)$")


def parse_grounding_prompt(prompt: str) -> tuple[dict[int, str], list[_PromptClaim]]:
    """Read the grounding prompt back into source blocks and claims.

    Line-oriented, because a source block contains blank lines of its own and so cannot be
    split on them. Reading it back rather than being handed the structure is the point: it
    is the harness's way of asserting that the prompt says what the validator meant, and it
    breaks loudly if the format changes — which is what a corpus guarding a prompt should
    do.

    **Inside a fence, nothing is a heading**, and the closing line has to carry the marker
    the opening line did. That is the same rule the model is given, so a source item that
    contains its own ``CLAIM 0`` lines is read here exactly as the model is told to read it
    — as prose. A harness that fell for the splice would score a case against a prompt
    nobody sent.
    """
    blocks: dict[int, list[str]] = {}
    claims: list[_PromptClaim] = []
    open_block: tuple[int, str] | None = None
    for line in prompt.splitlines():
        if open_block is not None:
            number, marker = open_block
            if line == f"END SOURCE {number} {marker}":
                open_block = None
            else:
                blocks[number].append(line)
            continue
        begin = _BEGIN_SOURCE.match(line)
        if begin is not None:
            open_block = (int(begin.group(1)), begin.group(2))
            blocks[open_block[0]] = []
            continue
        claim_heading = _CLAIM_HEADING.match(line)
        if claim_heading is not None:
            claims.append(_PromptClaim(index=int(claim_heading.group(1))))
            continue
        if claims:
            if line.startswith("SPOKEN: "):
                claims[-1].spoken = line.removeprefix("SPOKEN: ")
            elif line.startswith("SOURCE: "):
                claims[-1].source = int(line.removeprefix("SOURCE: "))
    if open_block is not None:
        raise ValueError(f"grounding prompt: SOURCE {open_block[0]} was never closed")
    return {number: "\n".join(lines).strip() for number, lines in blocks.items()}, claims


def source_blocks(prompts: Sequence[str]) -> int:
    """How many distinct source blocks were sent across every call of one validation."""
    return len(_blocks(prompts))


def evidence_shown(prompts: Sequence[str]) -> str:
    """Just the source blocks, which is the whole of what a claim may be grounded in.

    Deliberately not the rest of the prompt: the spoken text of a claim contains the very
    figure a case is asking about, so a substring check over the raw prompt would find a
    fabricated number in the fabrication itself and call it evidence.
    """
    return "\n\n".join(sorted(_blocks(prompts)))


def _blocks(prompts: Sequence[str]) -> set[str]:
    return {block for prompt in prompts for block in parse_grounding_prompt(prompt)[0].values()}
