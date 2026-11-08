"""Read public HTML metadata without executing website code."""
import json5
import json
import re


def literal_end(text, start):
    depth, quote, escape = 0, None, False
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if escape:
                escape = False
            elif c == '\\':
                escape = True
            elif c == quote:
                quote = None
        elif c in ('"', "'"):
            quote = c
        elif c in '{[':
            depth += 1
        elif c in '}]':
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError('Unterminated metadata object')


def object_at(text, start):
    end = literal_end(text, start)
    literal = text[start:end]
    wrapper = text.rfind('(function(', 0, start)
    if wrapper >= 0 and re.match(r'\(function\([a-zA-Z0-9_,]+\)\{return \{type:"data",data:$', text[wrapper:start]):
        names = text[wrapper + 10:text.index(')', wrapper)].split(',')
        call = text.find('}}}(', end)
        if call < 0:
            raise ValueError('Unrecognized serialized HTML wrapper')
        args_start = call + 4
        # Find the argument-list closing parenthesis without evaluating JavaScript.
        quote, escape = None, False
        for i in range(args_start, len(text)):
            c = text[i]
            if quote:
                if escape: escape = False
                elif c == '\\': escape = True
                elif c == quote: quote = None
            elif c in ('"', "'"): quote = c
            elif c == ')': break
        values = json5.loads('[' + text[args_start:i] + ']')
        bindings = dict(zip(names, values, strict=True))
        token = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\b[A-Za-z_$][\w$]*\b'
        def replace(m):
            name = m[0]
            if name in bindings and not literal[m.end():].lstrip().startswith(':'):
                return json.dumps(bindings[name])
            return name
        literal = re.sub(token, replace, literal)
    # Search snippets may be streamed promises; the result identities are already
    # literal HTML data. Preserve their unresolved status without executing code.
    literal, deferred = re.subn(r'\bsnippets:__sveltekit_[A-Za-z0-9_]+\.defer\(\d+\)',
                               'snippets:null', literal)
    value = json5.loads(literal)
    if deferred:
        value['_deferred_snippets'] = True
    return value


def metadata(text):
    result = {}
    for marker in ('{landing:{', '{pub:{'):
        start = text.find(marker)
        if start >= 0:
            result.update(object_at(text, start))
    start = text.find('{state:{')
    if start >= 0:
        result.update(object_at(text, start))
    if result:
        return result
    raise ValueError('Public HTML metadata layout changed or access challenge returned')
