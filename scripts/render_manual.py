#!/usr/bin/env python3
"""Render the manual's small Markdown subset without dependencies.

Markdown is the source of truth. Commit the HTML for offline builds and use
--check to detect stale output. Unsupported blocks are refused explicitly.
"""
import argparse
from html import escape
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def slug(text):
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def inline(text):
    tokens = []

    def token(value):
        tokens.append(value)
        return "\x00" + str(len(tokens) - 1) + "\x00"

    def link(match):
        label, url = match.group(1), match.group(2)
        if not re.fullmatch(r"(?:[A-Za-z0-9_./#:-]|%[0-9A-Fa-f]{2})+", url):
            raise ValueError("Unsupported manual link")
        if ":" in url and not url.startswith(("http://", "https://")):
            raise ValueError("Unsafe manual link")
        attributes = ' target="_blank" rel="noopener noreferrer"' if url.startswith("https://") else ""
        return token('<a href="' + escape(url, quote=True) + '"' + attributes + '>' + escape(label) + '</a>')

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, text)
    text = re.sub(r"\x60([^\x60]+)\x60", lambda m: token("<code>" + escape(m[1]) + "</code>"), text)
    text = escape(text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m[1])], text)


def render(source):
    lines, blocks, index = source.splitlines(), [], 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = re.fullmatch(r"(#{1,3}) (.+)", line)
        image = re.fullmatch(r"!\[([^\]]+)\]\((images/manual-[a-z-]+\.png)\)", line)
        if heading:
            level, text = len(heading[1]), heading[2]
            blocks.append(f'<h{level} id="{slug(text)}">{inline(text)}</h{level}>')
        elif image:
            alt, url = image.groups()
            blocks.append('<figure><a href="' + url + '"><img src="' + url +
                          '" alt="' + escape(alt, quote=True) +
                          '"></a><figcaption>' + escape(alt) + '</figcaption></figure>')
        elif line.startswith("| "):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append([cell.strip() for cell in lines[index].strip("|").split("|")])
                index += 1
            if len(rows) < 3 or not all(re.fullmatch(r":?-+:?", cell) for cell in rows[1]):
                raise ValueError("Invalid manual table")
            if any(len(row) != len(rows[0]) for row in rows[2:]):
                raise ValueError("Inconsistent manual table")
            head = "".join('<th scope="col">' + inline(cell) + '</th>' for cell in rows[0])
            body = "".join("<tr>" + "".join("<td>" + inline(cell) + "</td>" for cell in row) + "</tr>"
                           for row in rows[2:])
            blocks.append('<div class="table-wrap" tabindex="0" role="region" aria-label="Reference table">'
                          '<table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>')
            continue
        elif line.startswith("> "):
            blocks.append('<aside class="notice" aria-label="Important"><p>' + inline(line[2:]) + '</p></aside>')
        elif line.startswith("- ") or re.match(r"\d+\. ", line):
            ordered = not line.startswith("- ")
            pattern = r"\d+\. (.+)" if ordered else r"- (.+)"
            items = []
            start = int(line.split(".", 1)[0]) if ordered else 1
            while index < len(lines) and (match := re.fullmatch(pattern, lines[index])):
                items.append("<li>" + inline(match[1]) + "</li>")
                index += 1
            tag = "ol" if ordered else "ul"
            attributes = f' start="{start}"' if ordered and start != 1 else ""
            blocks.append(f"<{tag}{attributes}>" + "".join(items) + f"</{tag}>")
            continue
        else:
            paragraph = []
            while index < len(lines) and lines[index].strip():
                if lines[index].startswith(("#", "!", ">", "|", "- ", "    ", "~~~", chr(96) * 3)):
                    raise ValueError("Unsupported manual block")
                paragraph.append(lines[index])
                index += 1
            blocks.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            continue
        index += 1
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            '<title>SAT-IP Receiver — User Manual</title>\n'
            '<link rel="stylesheet" href="user-manual.css">\n'
            '<link rel="icon" href="images/manual-icon.png" type="image/png">\n</head>\n<body>\n'
            '<a class="skip-link" href="#manual">Skip to manual</a>\n'
            '<header class="manual-header"><span>SAT-IP · USER MANUAL</span>'
            '<a href="/" target="_blank" rel="noopener">Open receiver</a></header>\n'
            '<main id="manual">\n' + "\n".join(blocks) + '\n</main>\n'
            '<footer>English · Print or save as PDF using your browser. '
            'Select an illustration to view it at full size. Linked technical references are plain-text guides; optional software/hardware website links require internet access.</footer>\n'
            '</body>\n</html>\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "doc/user-manual.html"
    result = render((ROOT / "doc/user-manual.md").read_text())
    if args.check:
        if not target.exists() or target.read_text() != result:
            parser.error("Manual HTML is stale; run python3 scripts/render_manual.py")
    else:
        target.write_text(result)


if __name__ == "__main__":
    main()
