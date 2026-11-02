"""PINN/PDE 相关性规则。只依据英文标题与摘要，不调用模型。"""

from __future__ import annotations

import re

from .models import Decision, Paper

# 先匹配完整表达，再匹配缩写；缩写单独命中时按“语境不明”处理。
_PINN_FULL_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "physics-informed neural network",
        r"physics[-\s]?informed\s+neural\s+networks?",
    ),
    (
        "physics-informed deep learning",
        r"physics[-\s]?informed\s+deep\s+learning",
    ),
    (
        "physics-constrained neural network",
        r"physics[-\s]?constrained\s+neural\s+networks?",
    ),
    (
        "physics-informed machine learning",
        r"physics[-\s]?informed\s+machine\s+learning",
    ),
)

_PINN_ABBR_RE = re.compile(r"\bpinns?\b", re.IGNORECASE)

_PDE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "partial differential equation",
        r"partial[-\s]?differential\s+equations?",
    ),
    ("PDE", r"\bpdes?\b"),
    ("governing equation", r"governing\s+equations?"),
    ("forward problem", r"forward\s+problems?"),
    ("inverse problem", r"inverse\s+problems?"),
    ("Navier-Stokes", r"navier[-\s]?stokes"),
    ("Burgers equation", r"\bburgers(?:'|’)?\s+equations?"),
    ("heat equation", r"\bheat\s+equations?"),
    ("wave equation", r"\bwave\s+equations?"),
    ("Poisson equation", r"\bpoisson(?:'s)?\s+equations?"),
    ("diffusion equation", r"\bdiffusion\s+equations?"),
    ("advection equation", r"\badvection(?:[-\s]diffusion)?\s+equations?"),
    ("elliptic equation", r"\belliptic\s+(?:pde|equation|problem)s?"),
    ("parabolic equation", r"\bparabolic\s+(?:pde|equation|problem)s?"),
    ("hyperbolic equation", r"\bhyperbolic\s+(?:pde|equation|problem)s?"),
    ("shallow water equation", r"\bshallow[-\s]water\s+equations?"),
)

_ODE_RE = re.compile(
    r"\bordinary\s+differential\s+equations?\b|\bodes?\b", re.IGNORECASE
)
_REVIEW_TITLE_RE = re.compile(
    r"\b(review|survey|tutorial|overview)\b", re.IGNORECASE
)
_REVIEW_ABSTRACT_RE = re.compile(
    r"\b("
    r"this\s+(?:paper|article|work)\s+(?:reviews|surveys)"
    r"|this\s+(?:paper|article|work)\s+is\s+a\s+(?:review|survey)"
    r"|a\s+(?:systematic\s+)?(?:review|survey)\s+(?:of|on)"
    r"|systematic\s+review"
    r")\b",
    re.IGNORECASE,
)
_NEURAL_OPERATOR_RE = re.compile(
    r"\b(fourier\s+neural\s+operator|neural\s+operator|deeponet|fno)\b",
    re.IGNORECASE,
)


def _matches(text: str, patterns: tuple[tuple[str, str], ...]) -> list[str]:
    found: list[str] = []
    for label, pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE) and label not in found:
            found.append(label)
    return found


def _is_english(text: str) -> bool:
    if not text.strip():
        return False
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    ascii_letters = sum(
        1 for char in text if char.isascii() and char.isalpha()
    )
    if cjk >= 5:
        return False
    return ascii_letters >= 20


def is_review(title: str, abstract: str) -> bool:
    return bool(
        _REVIEW_TITLE_RE.search(title or "")
        or _REVIEW_ABSTRACT_RE.search(abstract or "")
    )


def decide(paper: Paper) -> Decision:
    text = f"{paper.title}\n{paper.abstract}"
    if not _is_english(text):
        return Decision(False, "not_english", ())

    if is_review(paper.title, paper.abstract):
        return Decision(False, "review", ())

    full_hits = _matches(text, _PINN_FULL_PATTERNS)
    abbr_hit = bool(_PINN_ABBR_RE.search(text))
    pde_hits = _matches(text, _PDE_PATTERNS)
    ode_hit = bool(_ODE_RE.search(text))
    operator_hit = bool(_NEURAL_OPERATOR_RE.search(text))

    matched: list[str] = list(full_hits)
    if abbr_hit:
        matched.append("PINN")
    matched.extend(pde_hits)

    if not full_hits and not abbr_hit:
        return Decision(False, "not_pinn", tuple(matched))

    if ode_hit and not pde_hits:
        return Decision(False, "no_pde_context", tuple(matched))

    if not pde_hits:
        if full_hits:
            return Decision(False, "no_pde_context", tuple(matched))
        # 只有 PINN/PINNs 缩写，无法确认 PDE、正/反问题或方法语境。
        return Decision(False, "insufficient_context", tuple(matched))

    if not full_hits and abbr_hit and not pde_hits:
        # 理论上前面的分支已覆盖；保留显式判断以防规则顺序变化。
        return Decision(False, "insufficient_context", tuple(matched))

    if operator_hit and not (full_hits or abbr_hit):
        return Decision(False, "not_pinn", tuple(matched))

    return Decision(True, "included", tuple(dict.fromkeys(matched)))
