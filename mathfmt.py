"""Преобразование LaTeX-формул из ответов Gemini в читаемый Юникод.

Telegram не умеет ни LaTeX, ни MathML: `$$\\frac{p_2}{p_1}$$` приходит в чат
как есть и превращает ответ в мусор. Модуль разбирает простую математику
(дроби, корни, степени, греческие буквы, операторы) и отдаёт обычный текст:
`p₂/p₁`, `β_кр = (2/(k + 1))^(k/(k - 1))`.
"""

from __future__ import annotations

import re

GREEK = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
}

SYMBOLS = {
    "cdot": "·",
    "times": "×",
    "div": "÷",
    "pm": "±",
    "mp": "∓",
    "approx": "≈",
    "neq": "≠",
    "ne": "≠",
    "leq": "≤",
    "le": "≤",
    "geq": "≥",
    "ge": "≥",
    "ll": "≪",
    "gg": "≫",
    "infty": "∞",
    "partial": "∂",
    "sum": "Σ",
    "prod": "∏",
    "int": "∫",
    "to": "→",
    "rightarrow": "→",
    "leftarrow": "←",
    "Rightarrow": "⇒",
    "equiv": "≡",
    "sim": "∼",
    "propto": "∝",
    "angle": "∠",
    "degree": "°",
    "circ": "°",
    "prime": "′",
    "ldots": "…",
    "dots": "…",
    "quad": "  ",
    "qquad": "    ",
    " ": " ",
    "\\": "\n",
    ",": " ",
    ";": " ",
    ":": " ",
    "!": "",
    "{": "{",
    "}": "}",
    "%": "%",
    "&": " ",
    "_": "_",
    "#": "#",
    "$": "$",
}

FUNCTIONS = {
    "ln",
    "log",
    "lg",
    "sin",
    "cos",
    "tan",
    "cot",
    "exp",
    "max",
    "min",
    "lim",
    "det",
    "diag",
}

TEXT_COMMANDS = {"text", "mathrm", "mathbf", "mathit", "mathbb", "mathcal", "operatorname", "textnormal", "textrm", "textbf"}
SPACE_COMMANDS = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr"}

SUPERSCRIPT = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "i": "ⁱ",
    "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ", "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ",
    "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
}

SUBSCRIPT = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ",
    "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}

_DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_BRACKET_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_PAREN_RE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
_INLINE_RE = re.compile(r"(?<!\\)\$([^$\n]+?)\$")
_EMPTY_LINES_RE = re.compile(r"\n{3,}")
_TRAILING_SPACES_RE = re.compile(r"[ \t]+\n")
_MARKER_RE = re.compile(r"[=≈≠<>^_\\]")


def _looks_like_math(content: str) -> bool:
    """Отличить формулу от денежной суммы вроде «100$ и 200$"."""
    if not content or content != content.strip():
        return False
    if content.isdigit():
        return False
    if _MARKER_RE.search(content):
        return True
    return " " not in content and len(content) <= 12


def latex_to_text(source: str) -> str:
    """Заменить LaTeX-формулы на читаемый текст, сохранив остальной Markdown."""

    def replace(expr: str) -> str:
        return _convert(expr).strip()

    text = source.replace("\r\n", "\n")
    text = _DISPLAY_RE.sub(lambda m: f"\n{replace(m.group(1))}\n", text)
    text = _BRACKET_RE.sub(lambda m: f"\n{replace(m.group(1))}\n", text)
    text = _PAREN_RE.sub(lambda m: replace(m.group(1)), text)
    text = _INLINE_RE.sub(
        lambda m: replace(m.group(1)) if _looks_like_math(m.group(1)) else m.group(0),
        text,
    )
    text = _TRAILING_SPACES_RE.sub("\n", text)
    text = _EMPTY_LINES_RE.sub("\n\n", text)
    return text.strip("\n")


def _command(source: str, index: int) -> tuple[str, int]:
    """Прочитать имя команды после обратного слэша."""
    position = index + 1
    if position >= len(source):
        return "", position
    if source[position].isalpha():
        end = position
        while end < len(source) and source[end].isalpha():
            end += 1
        return source[position:end], end
    return source[position], position + 1


def _group(source: str, index: int) -> tuple[str, int]:
    """Прочитать содержимое {...} с учётом вложенности."""
    depth = 0
    position = index
    while position < len(source):
        char = source[position]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index + 1 : position], position + 1
        position += 1
    return source[index + 1 :], len(source)


def _argument(source: str, index: int) -> tuple[str, int]:
    """Аргумент команды: группа {...} или один символ/команда."""
    position = index
    while position < len(source) and source[position] == " ":
        position += 1
    if position >= len(source):
        return "", position
    if source[position] == "{":
        return _group(source, position)
    if source[position] == "\\":
        name, end = _command(source, position)
        return "\\" + name, end
    return source[position], position + 1


def _wrap(expr: str) -> str:
    expr = expr.strip()
    if not expr:
        return expr
    if re.fullmatch(r"[\w\u0370-\u03ff.,°%]+", expr):
        return expr
    return f"({expr})"


def _script(text: str, *, superscript: bool) -> str:
    table = SUPERSCRIPT if superscript else SUBSCRIPT
    marker = "^" if superscript else "_"
    stripped = text.strip()
    if stripped and all(char in table for char in stripped):
        return "".join(table[char] for char in stripped)
    if len(stripped) == 1:
        return f"{marker}{stripped}"
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1]
    return f"{marker}({stripped})"


def _convert(source: str) -> str:
    out: list[str] = []
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char == "\\":
            name, index = _command(source, index)

            if name in {"frac", "dfrac", "tfrac"}:
                numerator, index = _argument(source, index)
                denominator, index = _argument(source, index)
                out.append(f"{_wrap(_convert(numerator))}/{_wrap(_convert(denominator))}")
            elif name == "sqrt":
                modifier = ""
                if index < length and source[index] == "[":
                    end = source.find("]", index)
                    if end != -1:
                        modifier = _convert(source[index + 1 : end]).strip()
                        index = end + 1
                argument, index = _argument(source, index)
                root = _script(modifier, superscript=True) if modifier else ""
                out.append(f"{root}√({_convert(argument).strip()})")
            elif name in TEXT_COMMANDS:
                argument, index = _argument(source, index)
                out.append(_convert(argument))
            elif name in {"begin", "end"}:
                _, index = _argument(source, index)
            elif name in GREEK:
                out.append(GREEK[name])
                # \Delta T -> ΔT: пробел после команды был только терминатором.
                if (
                    index < length
                    and source[index] == " "
                    and index + 1 < length
                    and source[index + 1].isalpha()
                ):
                    index += 1
            elif name in SYMBOLS:
                out.append(SYMBOLS[name])
            elif name in FUNCTIONS:
                out.append(name)
            elif name in SPACE_COMMANDS:
                continue
            else:
                out.append(name)

        elif char in "^_":
            index += 1
            argument, index = _argument(source, index)
            out.append(_script(_convert(argument), superscript=char == "^"))

        elif char == "{":
            group, index = _group(source, index)
            out.append(_convert(group))

        elif char == "}":
            index += 1

        elif char == "&":
            out.append(" ")
            index += 1

        else:
            out.append(char)
            index += 1

    result = "".join(out)
    result = result.replace("  ", " ")
    return result
