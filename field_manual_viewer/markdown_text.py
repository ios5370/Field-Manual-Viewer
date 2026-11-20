"""Safe, dependency-free helpers for the text-only Markdown releases."""
import html
import re

_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_FENCE = re.compile(r'^\s*(```|~~~)')
_IMAGE = re.compile(r'!\[([^\]]*)\]\([^)]+\)')
_LINK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_TAG = re.compile(r'<[^>]+>')
_FRONT_MATTER = re.compile(r'^---\s*\n(.*?)\n---\s*\n?', re.DOTALL)


def split_front_matter(text):
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    metadata = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(':')
        if separator:
            metadata[key.strip()] = value.strip().strip('"\'')
    return metadata, text[match.end():]


def _clean_title(value):
    value = _IMAGE.sub(r'\1', value)
    value = _LINK.sub(r'\1', value)
    value = re.sub(r'[*_`~]+', '', value)
    value = html.unescape(_TAG.sub(' ', value))
    return re.sub(r'\s+', ' ', value).strip()


def _slug(value, fallback='section'):
    value = re.sub(r'[^A-Za-z0-9]+', '-', _clean_title(value)).strip('-').lower()
    return (value[:64] or fallback)


def sections(text):
    """Split Markdown at ATX headings, preserving the original section text.

    The locator is stable for a given document and can be used by search hits.
    Code fences are not treated as heading content.
    """
    _, text = split_front_matter(text)
    lines = text.split('\n')
    result = []
    current_title = 'Document'
    current_lines = []
    current_level = 0
    in_fence = False

    def add():
        nonlocal current_lines, current_title, current_level
        body = '\n'.join(current_lines).strip('\n')
        if not body and not result:
            return
        index = len(result) + 1
        result.append({
            'locator': f'{index:03d}-{_slug(current_title)}',
            'title': current_title or 'Section',
            'level': current_level,
            'text': body,
        })
        current_lines = []

    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            current_lines.append(line)
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            if current_lines and any(part.strip() for part in current_lines):
                add()
            current_title = _clean_title(match.group(2)) or 'Section'
            current_level = len(match.group(1))
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines and any(part.strip() for part in current_lines):
        add()
    if not result:
        result.append({'locator': '001-document', 'title': 'Document',
                       'level': 0, 'text': text})
    return result


def plain_text(text):
    """Approximate readable text for FTS while keeping all substantive words."""
    _, value = split_front_matter(text)
    value = re.sub(r'```[^\n]*\n?', '', value)
    value = re.sub(r'~~~[^\n]*\n?', '', value)
    value = _IMAGE.sub(r'\1', value)
    value = _LINK.sub(r'\1', value)
    value = html.unescape(_TAG.sub(' ', value))
    value = re.sub(r'^\s{0,3}#{1,6}\s*', '', value, flags=re.MULTILINE)
    value = re.sub(r'^\s{0,3}>\s?', '', value, flags=re.MULTILINE)
    value = re.sub(r'^\s*[-+*]\s+', '', value, flags=re.MULTILINE)
    value = re.sub(r'^\s*\d+[.)]\s+', '', value, flags=re.MULTILINE)
    value = re.sub(r'^\s*\|?[\s:|-]+\|[\s:|-]*$', '', value, flags=re.MULTILINE)
    value = value.replace('|', ' ')
    value = re.sub(r'[*_`~]+', '', value)
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()
