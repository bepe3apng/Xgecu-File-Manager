"""Safe token highlighting for existing programmer messages."""
import re
from pathlib import Path

from kivy.utils import escape_markup, get_hex_from_color

TIME = re.compile(r"^\[\d{2}:\d{2}:\d{2}:\d{3}\]")
TOKENS = re.compile(
    r"(?P<time>^\[\d{2}:\d{2}:\d{2}:\d{3}\])"
    r"|(?P<level>\[(?:\+|\*|!|-)\]|\b(?:INFO|OK|WARN|ERROR)\b)"
    r"|(?P<address>\b0[xX][0-9a-fA-F]+\b)"
    r"|(?P<operation>\b(?:READ|WRITE|ERASE|VERIFY)\b)"
    r"|(?P<location>\b(?:Block|Page|LBA)\s*[:=]?\s*\d+\b)"
    r"|(?P<failure>\b(?:BAD_BLOCK|WRITE_FAILED|MISMATCH)\b)"
)


def log_level(line):
    message = TIME.sub("", line).strip()
    lower = message.lower()
    if (message.startswith(("[-]", "ERROR")) or
            re.search(r"\b(?:WRITE_FAILED|MISMATCH)\b", message)):
        return "ERROR"
    if message.startswith("[!]") and any(word in lower for word in (
            "ошиб", "тайм-аут", "не отвечает", "не найден", "не удалось")):
        return "ERROR"
    if (message.startswith(("[!]", "WARN")) or "предупреждение" in lower or
            "BAD_BLOCK" in message or "бэд-блок" in lower):
        return "WARN"
    if message.startswith(("[+]", "OK")):
        return "OK"
    return "INFO"


def format_log(value, colors, level="Все", query=""):
    def paint(value, role, bold=False):
        color = get_hex_from_color(colors[role])[1:7]
        value = escape_markup(value)
        if bold:
            value = "[b]" + value + "[/b]"
        return "[color=" + color + "]" + value + "[/color]"

    result = []
    for line in value.splitlines():
        severity = log_level(line)
        if level != "Все" and severity != level:
            continue
        if query.casefold() not in line.casefold():
            continue
        status_role = {"INFO": "blue", "OK": "green", "WARN": "orange", "ERROR": "red"}[severity]
        parts, end = [], 0
        for match in TOKENS.finditer(line):
            parts.append(escape_markup(line[end:match.start()]))
            kind = match.lastgroup
            role = {"time": "subtle", "level": status_role, "address": "primary",
                    "operation": "text", "location": "muted",
                    "failure": status_role}[kind]
            parts.append(paint(match.group(), role, kind in ("level", "operation", "failure")))
            end = match.end()
        parts.append(escape_markup(line[end:]))
        result.append("".join(parts))
    return "\n".join(result)


def console_font():
    """Use bundled Roboto Mono; Windows Consolas is an offline fallback."""
    bundled = Path(__file__).parent / "assets" / "RobotoMono-Regular.ttf"
    if bundled.is_file():
        return str(bundled)
    windows_font = Path("C:/Windows/Fonts/consola.ttf")
    return str(windows_font) if windows_font.is_file() else "Roboto"
