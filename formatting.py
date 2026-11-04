"""Markdown от Gemini → HTML, понятный Telegram, плюс разбивка на сообщения.

Telegram не умеет Markdown в общем виде: у него либо legacy-Markdown, который
ломается на любом неэкранированном символе, либо HTML с фиксированным набором
тегов. Поэтому ответ модели сначала превращается в HTML, а затем режется на
части по лимиту 4096 символов с корректным закрытием/переоткрытием тегов.
"""

from __future__ import annotations

import re

from mathfmt import latex_to_text

MAX_MESSAGE_LENGTH = 4096  # жёсткий лимит Telegram на одно сообщение
CHUNK_LIMIT = 3800  # запас на незакрытые теги и служебную разметку

_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(?!\s)([^\n]+?)(?<!\s)\*\*")
_BOLD_UNDER_RE = re.compile(r"(?<![\w_])__(?!\s)([^\n]+?)(?<!\s)__(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?!\s)([^\n]+?)(?<!\s)~~")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?![\s_])([^_\n]+?)(?<![\s_])_(?![\w_])")

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^<>]*)?>")
_LANGUAGE_RE = re.compile(r"[A-Za-z0-9+#._-]{1,20}")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """Инлайн-разметка одной строки (текст уже экранирован)."""
    if not text:
        return text

    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # Код и ссылки прячем, чтобы звёздочки внутри них не превращались в курсив.
    text = _INLINE_CODE_RE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), text)
    text = _LINK_RE.sub(
        lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text
    )

    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_UNDER_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_STAR_RE.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDER_RE.sub(r"<i>\1</i>", text)

    for index, fragment in enumerate(stash):
        text = text.replace(f"\x00{index}\x00", fragment)
    return text


def _clean_table_cell(cell: str) -> str:
    cell = re.sub(r"\[([^\]\n]+)\]\((?:[^)\n]*)\)", r"\1", cell)
    for marker in ("**", "__", "~~", "`", "*", "_"):
        cell = cell.replace(marker, "")
    return cell.strip()


def _render_table(rows: list[str]) -> str:
    """Markdown-таблицу показываем моноширинным блоком: Telegram таблиц не умеет."""
    grid: list[list[str]] = []
    for row in rows:
        if _TABLE_DIVIDER_RE.match(row):
            continue
        cells = [_clean_table_cell(cell) for cell in row.strip().strip("|").split("|")]
        grid.append(cells)
    if not grid:
        return ""

    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    column_widths = [max(len(row[index]) for row in grid) for index in range(width)]

    lines: list[str] = []
    for index, row in enumerate(grid):
        lines.append(" │ ".join(cell.ljust(column_widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0 and len(grid) > 1:
            lines.append("─".join("─" * (w + 2) for w in column_widths))
    return "<pre>" + "\n".join(lines) + "</pre>"


def _render_code_block(language: str, code: str) -> str:
    body = _escape(code.rstrip("\n"))
    language = language.strip().lstrip(".")
    if language and _LANGUAGE_RE.fullmatch(language):
        return f'<pre><code class="language-{language}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _render_text_block(block: str) -> str:
    """Построчная обработка обычного текста (без блоков кода)."""
    block = latex_to_text(block.strip("\n"))
    block = _escape(block)
    if not block.strip():
        return ""

    lines = block.split("\n")
    out: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]

        if _TABLE_ROW_RE.match(line):
            end = index
            while end < total and _TABLE_ROW_RE.match(lines[end]):
                end += 1
            rendered = _render_table(lines[index:end])
            out.append(rendered or "\n".join(_inline(row) for row in lines[index:end]))
            index = end
            continue

        if line.lstrip().startswith("&gt;"):
            end = index
            quoted: list[str] = []
            while end < total and lines[end].lstrip().startswith("&gt;"):
                quoted.append(lines[end].lstrip()[4:].lstrip())
                end += 1
            body = "\n".join(_inline(part) for part in quoted).strip()
            if body:
                out.append(f"<blockquote>{body}</blockquote>")
            index = end
            continue

        if _HORIZONTAL_RULE_RE.match(line):
            out.append("──────────")
            index += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>")
            index += 1
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            out.append(f"{bullet.group(1)}• {_inline(bullet.group(2))}")
            index += 1
            continue

        out.append(_inline(line))
        index += 1

    return "\n".join(out)


def markdown_to_html(text: str) -> str:
    """Сконвертировать Markdown-ответ модели в HTML для Telegram."""
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    position = 0
    for match in _FENCE_RE.finditer(source):
        parts.append(_render_text_block(source[position : match.start()]))
        parts.append(_render_code_block(match.group(1), match.group(2)))
        position = match.end()
    parts.append(_render_text_block(source[position:]))
    return "\n".join(part for part in parts if part).strip()


def _tokenize(html: str) -> list[tuple[str, str, str]]:
    """Разложить HTML на токены: ("text"|"open"|"close", кусок, имя тега)."""
    tokens: list[tuple[str, str, str]] = []
    position = 0
    for match in _TAG_RE.finditer(html):
        if match.start() > position:
            tokens.append(("text", html[position : match.start()], ""))
        raw = match.group(0)
        name = match.group(1).lower()
        tokens.append(("close" if raw.startswith("</") else "open", raw, name))
        position = match.end()
    if position < len(html):
        tokens.append(("text", html[position:], ""))
    return tokens


def _split_text(text: str, room: int) -> list[str]:
    """Нарезать текст на куски не длиннее room, по возможности по переводам строк."""
    if len(text) <= room:
        return [text]

    pieces: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        candidate = current + line
        if len(candidate) <= room:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        while len(line) > room:
            pieces.append(line[:room])
            line = line[room:]
        current = line
    if current:
        pieces.append(current)
    return pieces


def render_chunks(markdown_text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Разбить ответ на сообщения, не разрывая HTML-теги."""
    limit = max(500, min(limit, MAX_MESSAGE_LENGTH - 96))
    html = markdown_to_html(markdown_text)
    if not html:
        return []
    if len(html) <= limit:
        return [html]

    chunks: list[str] = []
    buffer: list[str] = []
    open_tags: list[tuple[str, str]] = []  # (имя тега, открывающий тег целиком)
    remaining = limit

    def reopen() -> list[str]:
        return [tag for _, tag in open_tags]

    def flush() -> None:
        nonlocal buffer, remaining
        body = "".join(buffer).strip()
        if body:
            chunks.append(body + "".join(f"</{name}>" for name, _ in reversed(open_tags)))
        buffer = reopen()
        remaining = limit - sum(len(tag) for tag in buffer)

    for kind, value, name in _tokenize(html):
        if kind == "text":
            for piece in _split_text(value, max(remaining - 8, 200)):
                if not piece:
                    continue
                if len(piece) > remaining and buffer:
                    flush()
                buffer.append(piece)
                remaining -= len(piece)
        elif kind == "open":
            buffer.append(value)
            open_tags.append((name, value))
            remaining -= len(value)
        else:
            buffer.append(value)
            for position in range(len(open_tags) - 1, -1, -1):
                if open_tags[position][0] == name:
                    del open_tags[position]
                    break
            remaining -= len(value)

    tail = "".join(buffer).strip()
    if tail:
        chunks.append(tail + "".join(f"</{name}>" for name, _ in reversed(open_tags)))
    return chunks or [html[:limit]]


def strip_tags(html: str) -> str:
    """Аварийный вариант: тот же текст, но без разметки."""
    text = re.sub(r"<[^>]+>", "", html)
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )
