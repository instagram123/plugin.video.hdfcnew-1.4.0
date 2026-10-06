# -*- coding: utf-8 -*-
"""JavaScript helpers for the hdfilmcehennemi embed players.

* ``unpack_packer(src)``  - Dean Edwards p.a.c.k.e.r ``eval(function(p,a,c,k,e,d)...)``
* ``JSInterpreter``       - a tiny ES5-subset interpreter used to run the
  randomized string decoders the Close / Rapidrame players use to hide the
  stream URL (``var x = f([...])`` -> ``jwplayer().setup({sources:[{file:x}]})``).

The decoders change identifier names, keys and payload on every request, so
instead of matching their constants with regexes we execute them, exactly as
the browser does. Only pure computation is supported (strings, numbers, arrays,
objects, functions, String/Array/Math built-ins, atob/btoa). DOM access or
unsupported syntax raises ``JSError`` so callers can fail cleanly.

Pure Python, no dependencies, works on Python 2.7 and 3.x.
"""
from __future__ import unicode_literals

import base64
import math
import random
import re

try:  # Python 2
    unichr
except NameError:  # Python 3
    unichr = chr

__all__ = ['JSError', 'JSInterpreter', 'UNDEFINED', 'unpack_packer', 'find_packed']


# --------------------------------------------------------------------------
# p.a.c.k.e.r
# --------------------------------------------------------------------------
_PACKED_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,[dr]\).*?\}\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
    r"\s*'((?:[^'\\]|\\.)*)'\s*\.split\(\s*'\|'\s*\)", re.S)
_B36 = '0123456789abcdefghijklmnopqrstuvwxyz'


def find_packed(src):
    """Return the list of p.a.c.k.e.r payloads found in ``src`` (unpacked)."""
    return [_unpack_match(m) for m in _PACKED_RE.finditer(src or '')]


def unpack_packer(src):
    """Unpack the first p.a.c.k.e.r blob in ``src``; '' if there is none."""
    found = find_packed(src)
    return found[0] if found else ''


def _unpack_match(m):
    payload = m.group(1).replace("\\'", "'").replace('\\\\', '\\')
    radix, count = int(m.group(2)), int(m.group(3))
    symtab = m.group(4).split('|')

    def encode(n):
        prefix = '' if n < radix else encode(n // radix)
        n %= radix
        return prefix + (unichr(n + 29) if n > 35 else _B36[n])

    lookup = {}
    for i in range(count):
        key = encode(i)
        lookup[key] = symtab[i] if i < len(symtab) and symtab[i] else key
    # JS \w is ASCII-only: without re.ASCII "Tü2zçe" would be one word.
    return re.sub(r'\b\w+\b', lambda w: lookup.get(w.group(0), w.group(0)), payload,
                  flags=re.ASCII if hasattr(re, 'ASCII') else 0)


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------
class JSError(Exception):
    pass


class JSThrow(JSError):
    def __init__(self, value):
        JSError.__init__(self, 'Uncaught %s' % _to_string(value))
        self.value = value


class _Undefined(object):
    __slots__ = ()

    def __repr__(self):
        return 'undefined'

    def __bool__(self):
        return False
    __nonzero__ = __bool__


UNDEFINED = _Undefined()
_NAN = float('nan')
_INF = float('inf')


class JSRegExp(object):
    def __init__(self, source, flags):
        self.source = source
        self.flags = flags
        self.is_global = 'g' in flags
        pyflags = re.ASCII if hasattr(re, 'ASCII') else 0
        if 'i' in flags:
            pyflags |= re.I
        if 'm' in flags:
            pyflags |= re.M
        if 's' in flags:
            pyflags |= re.S
        pattern = source.replace('[^]', r'[\s\S]')
        pattern = re.sub(r'\(\?<(?![=!])', '(?P<', pattern)
        try:
            self.regex = re.compile(pattern, pyflags)
        except re.error as exc:
            raise JSError('unsupported regex /%s/: %s' % (source, exc))
        self.last_index = 0


class Native(object):
    """A built-in function: ``fn(this, args)``."""
    __slots__ = ('fn', 'name')

    def __init__(self, fn, name=''):
        self.fn = fn
        self.name = name

    def call(self, this, args):
        return self.fn(this, args)


class JSFunction(object):
    __slots__ = ('interp', 'name', 'params', 'body', 'scope', 'arrow', 'expr_body')

    def __init__(self, interp, name, params, body, scope, arrow=False, expr_body=False):
        self.interp = interp
        self.name = name
        self.params = params
        self.body = body
        self.scope = scope
        self.arrow = arrow
        self.expr_body = expr_body

    def call(self, this, args):
        scope = Scope(self.scope)
        if not self.arrow:
            scope.vars['this'] = this
            scope.vars['arguments'] = list(args)
        for i, name in enumerate(self.params):
            scope.vars[name] = args[i] if i < len(args) else UNDEFINED
        interp = self.interp
        if self.expr_body:
            return interp.eval(self.body, scope)
        interp.hoist(self.body, scope)
        try:
            for stmt in self.body:
                interp.exec_stmt(stmt, scope)
        except _Return as ret:
            return ret.value
        return UNDEFINED


class Scope(object):
    __slots__ = ('vars', 'parent')

    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent

    def lookup(self, name):
        scope = self
        while scope is not None:
            if name in scope.vars:
                return scope.vars[name]
            scope = scope.parent
        if name == 'this':
            return UNDEFINED
        raise JSError('%s is not defined' % name)

    def assign(self, name, value):
        scope = self
        while True:
            if name in scope.vars or scope.parent is None:
                scope.vars[name] = value
                return
            scope = scope.parent


class _Return(Exception):
    def __init__(self, value):
        Exception.__init__(self)
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


# --------------------------------------------------------------------------
# Conversions (ECMAScript semantics for the parts we need)
# --------------------------------------------------------------------------
def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _norm(n):
    if isinstance(n, float) and n.is_integer() and abs(n) < 9007199254740992:
        return int(n)
    return n


def _to_number(v):
    if isinstance(v, bool):
        return 1 if v else 0
    if _is_num(v):
        return v
    if v is None:
        return 0
    if v is UNDEFINED:
        return _NAN
    if isinstance(v, (list, dict)):
        return _to_number(_to_primitive(v))
    if isinstance(v, str_types):
        s = v.strip()
        if not s:
            return 0
        try:
            if s[:2].lower() == '0x':
                return int(s[2:], 16)
            if s in ('Infinity', '+Infinity'):
                return _INF
            if s == '-Infinity':
                return -_INF
            if s.lower() in ('inf', '+inf', '-inf', 'nan', 'infinity', '-infinity'):
                return _NAN
            return _norm(float(s))
        except ValueError:
            return _NAN
    return _NAN


def _num_to_string(n, radix=10):
    if isinstance(n, float):
        if n != n:
            return 'NaN'
        if n in (_INF, -_INF):
            return 'Infinity' if n > 0 else '-Infinity'
        if n.is_integer() and abs(n) < 1e21:
            n = int(n)
    if isinstance(n, int):
        if radix == 10:
            return str(n)
        digits, neg, n = [], n < 0, abs(n)
        while True:
            n, r = divmod(n, radix)
            digits.append(_B36[r])
            if not n:
                break
        return ('-' if neg else '') + ''.join(reversed(digits))
    if radix == 10:
        text = repr(n)
        if 'e' in text:
            mant, exp = text.split('e')
            text = '%se%s%d' % (mant, '-' if int(exp) < 0 else '+', abs(int(exp)))
        return text
    whole = int(abs(n))
    frac = abs(n) - whole
    out = _num_to_string(whole, radix) + '.'
    for _ in range(20):
        frac *= radix
        d = int(frac)
        out += _B36[d]
        frac -= d
        if not frac:
            break
    return ('-' if n < 0 else '') + out


def _to_string(v):
    if isinstance(v, str_types):
        return v
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if _is_num(v):
        return _num_to_string(v)
    if v is None:
        return 'null'
    if v is UNDEFINED:
        return 'undefined'
    if isinstance(v, list):
        return ','.join('' if x is None or x is UNDEFINED else _to_string(x) for x in v)
    if isinstance(v, dict):
        return '[object Object]'
    if isinstance(v, JSRegExp):
        return '/%s/%s' % (v.source, v.flags)
    if isinstance(v, (JSFunction, Native)):
        return 'function %s() { [code] }' % (v.name or '')
    return '%s' % (v,)


def _to_primitive(v):
    if isinstance(v, (list, dict, JSRegExp, JSFunction, Native)):
        return _to_string(v)
    return v


def _truthy(v):
    if v is UNDEFINED or v is None or v is False:
        return False
    if v is True:
        return True
    if _is_num(v):
        return not (v == 0 or v != v)
    if isinstance(v, str_types):
        return len(v) > 0
    return True


def _to_int32(v):
    n = _to_number(v)
    if isinstance(n, float):
        if n != n or n in (_INF, -_INF):
            return 0
        n = int(n)
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n & 0x80000000 else n


def _to_uint32(v):
    return _to_int32(v) & 0xFFFFFFFF


def _to_int(v, default=0):
    if v is UNDEFINED:
        return default
    n = _to_number(v)
    if isinstance(n, float):
        if n != n:
            return 0
        if n in (_INF, -_INF):
            return int(math.copysign(2 ** 53, n))
        return int(n)
    return n


def _typeof(v):
    if v is UNDEFINED:
        return 'undefined'
    if v is None:
        return 'object'
    if isinstance(v, bool):
        return 'boolean'
    if _is_num(v):
        return 'number'
    if isinstance(v, str_types):
        return 'string'
    if isinstance(v, (JSFunction, Native)):
        return 'function'
    return 'object'


def _strict_equals(a, b):
    if _is_num(a) and _is_num(b):
        return a == b
    if isinstance(a, str_types) and isinstance(b, str_types):
        return a == b
    if isinstance(a, bool) and isinstance(b, bool):
        return a == b
    return a is b


def _loose_equals(a, b):
    if (a is None or a is UNDEFINED) and (b is None or b is UNDEFINED):
        return True
    if a is None or a is UNDEFINED or b is None or b is UNDEFINED:
        return False
    if _typeof(a) == _typeof(b):
        return _strict_equals(a, b)
    if isinstance(a, bool):
        return _loose_equals(_to_number(a), b)
    if isinstance(b, bool):
        return _loose_equals(a, _to_number(b))
    if _is_num(a) and isinstance(b, str_types):
        return a == _to_number(b)
    if isinstance(a, str_types) and _is_num(b):
        return _to_number(a) == b
    if isinstance(a, (list, dict)):
        return _loose_equals(_to_primitive(a), b)
    if isinstance(b, (list, dict)):
        return _loose_equals(a, _to_primitive(b))
    return False


try:
    str_types = (str, unicode)  # noqa: F821  (Python 2)
except NameError:
    str_types = (str,)


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------
_PUNCTUATORS = sorted((
    '>>>= ... === !== **= <<= >>= >>> ?. ?? ** => == != <= >= && || ++ -- += -= *= /= %= '
    '&= |= ^= << >> { } ( ) [ ] ; , < > + - * / % & | ^ ! ~ ? : = .').split(), key=len, reverse=True)
_NUM_RE = re.compile(r'0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?')
_NAME_RE = re.compile(r'[A-Za-z_$][\w$]*')
_REGEX_PREV_NAMES = {'return', 'typeof', 'case', 'do', 'else', 'in', 'of', 'new', 'delete',
                     'void', 'throw', 'instanceof'}
_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0'}


def _tokenize(src):
    tokens = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ' \t\r\n\x0b\x0c ﻿  ':
            i += 1
            continue
        if src.startswith('//', i):
            j = src.find('\n', i)
            i = n if j < 0 else j
            continue
        if src.startswith('/*', i):
            j = src.find('*/', i + 2)
            if j < 0:
                raise JSError('unterminated comment')
            i = j + 2
            continue
        if c.isdigit() or (c == '.' and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM_RE.match(src, i)
            text = m.group(0)
            if text[:2].lower() == '0x':
                value = int(text, 16)
            elif re.match(r'^\d+$', text):
                value = int(text)
            else:
                value = _norm(float(text))
            tokens.append(('num', value))
            i = m.end()
            continue
        if c in '"\'`':
            value, i = _read_string(src, i)
            tokens.append(('str', value))
            continue
        if c.isalpha() or c in '_$':
            m = _NAME_RE.match(src, i)
            if not m:
                raise JSError('unexpected character %r' % c)
            tokens.append(('name', m.group(0)))
            i = m.end()
            continue
        if c == '/' and _regex_allowed(tokens):
            value, i = _read_regex(src, i)
            tokens.append(('regex', value))
            continue
        for p in _PUNCTUATORS:
            if src.startswith(p, i):
                tokens.append(('punct', p))
                i += len(p)
                break
        else:
            raise JSError('unexpected character %r at %d' % (c, i))
    tokens.append(('eof', None))
    return tokens


def _regex_allowed(tokens):
    if not tokens:
        return True
    kind, value = tokens[-1]
    if kind == 'punct':
        return value not in (')', ']', '}')
    if kind == 'name':
        return value in _REGEX_PREV_NAMES
    return False


def _read_string(src, i):
    quote = src[i]
    i += 1
    out = []
    n = len(src)
    while i < n:
        c = src[i]
        if c == quote:
            return ''.join(out), i + 1
        if quote == '`' and src.startswith('${', i):
            raise JSError('template literal interpolation is not supported')
        if c == '\\':
            i += 1
            e = src[i] if i < n else ''
            if e in _ESCAPES and not (e == '0' and i + 1 < n and src[i + 1].isdigit()):
                out.append(_ESCAPES[e])
                i += 1
            elif e == 'x':
                out.append(unichr(int(src[i + 1:i + 3], 16)))
                i += 3
            elif e == 'u':
                if src[i + 1] == '{':
                    j = src.index('}', i)
                    out.append(unichr(int(src[i + 2:j], 16)))
                    i = j + 1
                else:
                    out.append(unichr(int(src[i + 1:i + 5], 16)))
                    i += 5
            elif e == '\r':
                i += 2 if src.startswith('\r\n', i) else 1
            elif e in '\n  ':
                i += 1
            else:
                out.append(e)
                i += 1
            continue
        if c in '\r\n' and quote != '`':
            raise JSError('unterminated string')
        out.append(c)
        i += 1
    raise JSError('unterminated string')


def _read_regex(src, i):
    j = i + 1
    in_class = False
    n = len(src)
    while j < n:
        c = src[j]
        if c == '\\':
            j += 2
            continue
        if c == '[':
            in_class = True
        elif c == ']':
            in_class = False
        elif c == '/' and not in_class:
            break
        elif c in '\r\n':
            raise JSError('unterminated regex')
        j += 1
    else:
        raise JSError('unterminated regex')
    m = re.compile(r'[a-z]*').match(src, j + 1)
    return (src[i + 1:j], m.group(0)), m.end()


# --------------------------------------------------------------------------
# Parser -> tuple based AST
# --------------------------------------------------------------------------
_BINARY_PREC = {
    '??': 1, '||': 2, '&&': 3, '|': 4, '^': 5, '&': 6,
    '==': 7, '!=': 7, '===': 7, '!==': 7,
    '<': 8, '>': 8, '<=': 8, '>=': 8, 'instanceof': 8, 'in': 8,
    '<<': 9, '>>': 9, '>>>': 9,
    '+': 10, '-': 10,
    '*': 11, '/': 11, '%': 11,
    '**': 12,
}
_ASSIGN_OPS = {'=', '+=', '-=', '*=', '/=', '%=', '<<=', '>>=', '>>>=', '&=', '|=', '^=', '**='}


class _Parser(object):
    def __init__(self, src):
        self.tokens = _tokenize(src)
        self.pos = 0

    # -- token helpers --------------------------------------------------
    def peek(self, offset=0):
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def at(self, value, kind=None):
        k, v = self.tokens[self.pos]
        return v == value and (kind is None or k == kind) and k in ('punct', 'name')

    def accept(self, value):
        if self.at(value):
            self.pos += 1
            return True
        return False

    def expect(self, value):
        if not self.accept(value):
            raise JSError('expected %r but found %r' % (value, self.peek()[1]))

    def name(self):
        kind, value = self.next()
        if kind != 'name':
            raise JSError('expected identifier, found %r' % (value,))
        return value

    def semicolon(self):
        self.accept(';')  # lenient automatic semicolon insertion

    # -- program / statements ------------------------------------------
    def program(self):
        body = []
        while self.peek()[0] != 'eof':
            body.append(self.statement())
        return body

    def block_body(self):
        self.expect('{')
        body = []
        while not self.at('}'):
            if self.peek()[0] == 'eof':
                raise JSError('unexpected end of input')
            body.append(self.statement())
        self.expect('}')
        return body

    def statement(self):
        kind, value = self.peek()
        if kind == 'punct':
            if value == '{':
                return ('block', self.block_body())
            if value == ';':
                self.next()
                return ('empty',)
        if kind == 'name':
            if value in ('var', 'let', 'const'):
                self.next()
                decl = self.var_declarations()
                self.semicolon()
                return decl
            if value == 'function' and self.peek(1)[0] == 'name':
                self.next()
                name = self.name()
                params, body = self.function_rest()
                return ('funcdecl', name, params, body)
            if value == 'return':
                self.next()
                if self.at(';') or self.at('}') or self.peek()[0] == 'eof':
                    self.semicolon()
                    return ('return', None)
                expr = self.expression()
                self.semicolon()
                return ('return', expr)
            if value == 'if':
                self.next()
                self.expect('(')
                test = self.expression()
                self.expect(')')
                cons = self.statement()
                alt = self.statement() if self.accept('else') else None
                return ('if', test, cons, alt)
            if value == 'for':
                return self.for_statement()
            if value == 'while':
                self.next()
                self.expect('(')
                test = self.expression()
                self.expect(')')
                return ('while', test, self.statement())
            if value == 'do':
                self.next()
                body = self.statement()
                self.expect('while')
                self.expect('(')
                test = self.expression()
                self.expect(')')
                self.semicolon()
                return ('dowhile', body, test)
            if value in ('break', 'continue'):
                self.next()
                self.semicolon()
                return (value,)
            if value == 'throw':
                self.next()
                expr = self.expression()
                self.semicolon()
                return ('throw', expr)
            if value == 'try':
                return self.try_statement()
            if value == 'switch':
                return self.switch_statement()
        expr = self.expression()
        self.semicolon()
        return ('expr', expr)

    def var_declarations(self):
        decls = []
        while True:
            name = self.name()
            init = self.assignment() if self.accept('=') else None
            decls.append((name, init))
            if not self.accept(','):
                return ('var', decls)

    def for_statement(self):
        self.next()
        self.expect('(')
        init = None
        if not self.at(';'):
            if self.peek()[1] in ('var', 'let', 'const') and self.peek()[0] == 'name':
                self.next()
                init = self.var_declarations()
            else:
                init = ('expr', self.expression(no_in=True))
            if self.at('in') or self.at('of'):
                raise JSError('for-in/for-of loops are not supported')
        self.expect(';')
        test = None if self.at(';') else self.expression()
        self.expect(';')
        update = None if self.at(')') else self.expression()
        self.expect(')')
        return ('for', init, test, update, self.statement())

    def try_statement(self):
        self.next()
        block = self.block_body()
        param, handler, final = None, None, None
        if self.accept('catch'):
            if self.accept('('):
                param = self.name()
                self.expect(')')
            handler = self.block_body()
        if self.accept('finally'):
            final = self.block_body()
        return ('try', block, param, handler, final)

    def switch_statement(self):
        self.next()
        self.expect('(')
        disc = self.expression()
        self.expect(')')
        self.expect('{')
        cases = []
        while not self.accept('}'):
            if self.accept('default'):
                test = None
            else:
                self.expect('case')
                test = self.expression()
            self.expect(':')
            body = []
            while not (self.at('case') or self.at('default') or self.at('}')):
                body.append(self.statement())
            cases.append((test, body))
        return ('switch', disc, cases)

    def function_rest(self):
        self.expect('(')
        params = []
        while not self.accept(')'):
            params.append(self.name())
            if not self.at(')'):
                self.expect(',')
        return params, self.block_body()

    # -- expressions -----------------------------------------------------
    def expression(self, no_in=False):
        expr = self.assignment(no_in)
        if self.at(','):
            exprs = [expr]
            while self.accept(','):
                exprs.append(self.assignment(no_in))
            return ('seq', exprs)
        return expr

    def assignment(self, no_in=False):
        arrow = self.try_arrow()
        if arrow is not None:
            return arrow
        left = self.conditional(no_in)
        kind, value = self.peek()
        if kind == 'punct' and value in _ASSIGN_OPS:
            if left[0] not in ('name', 'member'):
                raise JSError('invalid assignment target')
            self.next()
            return ('assign', value, left, self.assignment(no_in))
        return left

    def try_arrow(self):
        kind, value = self.peek()
        if kind == 'name' and self.peek(1) == ('punct', '=>'):
            self.pos += 2
            return self.arrow_body([value])
        if (kind, value) != ('punct', '('):
            return None
        depth, j = 0, self.pos
        while True:
            k, v = self.tokens[j]
            if k == 'eof':
                return None
            if k == 'punct' and v in ('(', '[', '{'):
                depth += 1
            elif k == 'punct' and v in (')', ']', '}'):
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if self.tokens[j + 1] != ('punct', '=>'):
            return None
        self.next()
        params = []
        while not self.accept(')'):
            params.append(self.name())
            if not self.at(')'):
                self.expect(',')
        self.expect('=>')
        return self.arrow_body(params)

    def arrow_body(self, params):
        if self.at('{'):
            return ('func', None, params, self.block_body(), True, False)
        return ('func', None, params, self.assignment(), True, True)

    def conditional(self, no_in=False):
        test = self.binary(0, no_in)
        if self.accept('?'):
            cons = self.assignment()
            self.expect(':')
            return ('cond', test, cons, self.assignment(no_in))
        return test

    def binary(self, min_prec, no_in=False):
        left = self.unary()
        while True:
            kind, op = self.peek()
            if kind not in ('punct', 'name') or op not in _BINARY_PREC or (no_in and op == 'in'):
                return left
            prec = _BINARY_PREC[op]
            if prec <= min_prec and not (op == '**' and prec == min_prec):
                return left
            self.next()
            right = self.binary(prec - 1 if op == '**' else prec, no_in)
            if op in ('&&', '||', '??'):
                left = ('logical', op, left, right)
            else:
                left = ('binary', op, left, right)

    def unary(self):
        kind, value = self.peek()
        if kind == 'punct' and value in ('!', '-', '+', '~'):
            self.next()
            return ('unary', value, self.unary())
        if kind == 'punct' and value in ('++', '--'):
            self.next()
            return ('update', value, True, self.unary())
        if kind == 'name' and value in ('typeof', 'void', 'delete'):
            self.next()
            return ('unary', value, self.unary())
        expr = self.postfix()
        return expr

    def postfix(self):
        expr = self.call_member()
        kind, value = self.peek()
        if kind == 'punct' and value in ('++', '--'):
            self.next()
            return ('update', value, False, expr)
        return expr

    def call_member(self):
        if self.at('new', 'name'):
            self.next()
            callee = self.member_only()
            args = self.arguments() if self.at('(') else []
            expr = ('new', callee, args)
        else:
            expr = self.primary()
        while True:
            if self.accept('.') or self.accept('?.'):
                expr = ('member', expr, ('str', self.name()), False)
            elif self.accept('['):
                prop = self.expression()
                self.expect(']')
                expr = ('member', expr, prop, True)
            elif self.at('('):
                expr = ('call', expr, self.arguments())
            else:
                return expr

    def member_only(self):
        expr = self.primary()
        while True:
            if self.accept('.'):
                expr = ('member', expr, ('str', self.name()), False)
            elif self.accept('['):
                prop = self.expression()
                self.expect(']')
                expr = ('member', expr, prop, True)
            else:
                return expr

    def arguments(self):
        self.expect('(')
        args = []
        while not self.accept(')'):
            args.append(self.assignment())
            if not self.at(')'):
                self.expect(',')
        return args

    def primary(self):
        kind, value = self.next()
        if kind == 'num':
            return ('num', value)
        if kind == 'str':
            return ('str', value)
        if kind == 'regex':
            return ('regex', value[0], value[1])
        if kind == 'name':
            if value == 'function':
                name = self.name() if self.peek()[0] == 'name' else None
                params, body = self.function_rest()
                return ('func', name, params, body, False, False)
            if value == 'true':
                return ('lit', True)
            if value == 'false':
                return ('lit', False)
            if value == 'null':
                return ('lit', None)
            if value == 'undefined':
                return ('lit', UNDEFINED)
            if value == 'this':
                return ('name', 'this')
            return ('name', value)
        if kind == 'punct':
            if value == '(':
                expr = self.expression()
                self.expect(')')
                return expr
            if value == '[':
                elems = []
                while not self.accept(']'):
                    if self.at(','):
                        self.next()
                        elems.append(('lit', UNDEFINED))
                        continue
                    elems.append(self.assignment())
                    if not self.at(']'):
                        self.expect(',')
                return ('array', elems)
            if value == '{':
                props = []
                while not self.accept('}'):
                    k, v = self.next()
                    if k not in ('name', 'str', 'num'):
                        raise JSError('unsupported object key %r' % (v,))
                    key = _to_string(v)
                    if self.at(',') or self.at('}'):
                        props.append((key, ('name', key)))
                    else:
                        self.expect(':')
                        props.append((key, self.assignment()))
                    if not self.at('}'):
                        self.expect(',')
                return ('object', props)
        raise JSError('unexpected token %r' % (value,))


# --------------------------------------------------------------------------
# Interpreter
# --------------------------------------------------------------------------
class JSInterpreter(object):
    """Execute JS source and read back globals.

    >>> js = JSInterpreter()
    >>> js.run('function f(a){return a.join("").split("").reverse().join("")}; var x = f(["ab","c"]);')
    >>> js.get('x')
    'cba'
    """

    def __init__(self, max_steps=5000000):
        self.max_steps = max_steps
        self.steps = 0
        self.globals = Scope()
        self._install_globals()

    # -- public API ------------------------------------------------------
    def run(self, src, tolerant=False):
        """Execute a script. With ``tolerant`` every top-level statement runs
        independently and failures are skipped (useful for page scripts that
        mix decoders with DOM code)."""
        program = _Parser(src).program()
        self.hoist(program, self.globals, tolerant=tolerant)
        errors = []
        for stmt in program:
            if not tolerant:
                self.exec_stmt(stmt, self.globals)
                continue
            try:
                self.exec_stmt(stmt, self.globals)
            except (JSError, _Return, _Break, _Continue) as exc:
                errors.append(exc)
        return errors

    def evaluate(self, expr_src):
        parser = _Parser(expr_src)
        expr = parser.expression()
        return self.eval(expr, self.globals)

    def get(self, name, default=None):
        return self.globals.vars.get(name, default)

    # -- statements ------------------------------------------------------
    def hoist(self, body, scope, tolerant=False):
        for stmt in body:
            self._hoist_vars(stmt, scope)
            if stmt[0] == 'funcdecl':
                scope.vars[stmt[1]] = JSFunction(self, stmt[1], stmt[2], stmt[3], scope)

    def _hoist_vars(self, stmt, scope):
        kind = stmt[0]
        if kind == 'var':
            for name, _ in stmt[1]:
                if name not in scope.vars:
                    scope.vars[name] = UNDEFINED
        elif kind == 'block':
            for s in stmt[1]:
                self._hoist_vars(s, scope)
        elif kind == 'if':
            self._hoist_vars(stmt[2], scope)
            if stmt[3] is not None:
                self._hoist_vars(stmt[3], scope)
        elif kind == 'for':
            if stmt[1] is not None:
                self._hoist_vars(stmt[1], scope)
            self._hoist_vars(stmt[4], scope)
        elif kind in ('while',):
            self._hoist_vars(stmt[2], scope)
        elif kind == 'dowhile':
            self._hoist_vars(stmt[1], scope)
        elif kind == 'try':
            for part in (stmt[1], stmt[3], stmt[4]):
                for s in part or ():
                    self._hoist_vars(s, scope)
        elif kind == 'switch':
            for _, body in stmt[2]:
                for s in body:
                    self._hoist_vars(s, scope)

    def _tick(self):
        self.steps += 1
        if self.steps > self.max_steps:
            raise JSError('step limit exceeded')

    def exec_stmt(self, stmt, scope):
        kind = stmt[0]
        if kind == 'expr':
            self.eval(stmt[1], scope)
        elif kind == 'var':
            for name, init in stmt[1]:
                if init is not None:
                    value = self.eval(init, scope)
                    if isinstance(value, JSFunction) and value.name is None:
                        value.name = name
                    scope.assign(name, value)
        elif kind == 'return':
            raise _Return(UNDEFINED if stmt[1] is None else self.eval(stmt[1], scope))
        elif kind == 'if':
            if _truthy(self.eval(stmt[1], scope)):
                self.exec_stmt(stmt[2], scope)
            elif stmt[3] is not None:
                self.exec_stmt(stmt[3], scope)
        elif kind == 'for':
            _, init, test, update, body = stmt
            if init is not None:
                self.exec_stmt(init, scope)
            while test is None or _truthy(self.eval(test, scope)):
                self._tick()
                try:
                    self.exec_stmt(body, scope)
                except _Break:
                    break
                except _Continue:
                    pass
                if update is not None:
                    self.eval(update, scope)
        elif kind == 'block':
            for s in stmt[1]:
                self.exec_stmt(s, scope)
        elif kind == 'while':
            while _truthy(self.eval(stmt[1], scope)):
                self._tick()
                try:
                    self.exec_stmt(stmt[2], scope)
                except _Break:
                    break
                except _Continue:
                    pass
        elif kind == 'dowhile':
            while True:
                self._tick()
                try:
                    self.exec_stmt(stmt[1], scope)
                except _Break:
                    break
                except _Continue:
                    pass
                if not _truthy(self.eval(stmt[2], scope)):
                    break
        elif kind == 'break':
            raise _Break()
        elif kind == 'continue':
            raise _Continue()
        elif kind == 'funcdecl':
            scope.vars[stmt[1]] = JSFunction(self, stmt[1], stmt[2], stmt[3], scope)
        elif kind == 'empty':
            pass
        elif kind == 'throw':
            raise JSThrow(self.eval(stmt[1], scope))
        elif kind == 'try':
            self._exec_try(stmt, scope)
        elif kind == 'switch':
            self._exec_switch(stmt, scope)
        else:
            raise JSError('unsupported statement %s' % kind)

    def _exec_try(self, stmt, scope):
        _, block, param, handler, final = stmt
        try:
            try:
                for s in block:
                    self.exec_stmt(s, scope)
            except JSError as exc:
                if handler is None:
                    raise
                if param:
                    scope.vars[param] = exc.value if isinstance(exc, JSThrow) else {'message': '%s' % exc}
                for s in handler:
                    self.exec_stmt(s, scope)
        finally:
            if final:
                for s in final:
                    self.exec_stmt(s, scope)

    def _exec_switch(self, stmt, scope):
        disc = self.eval(stmt[1], scope)
        cases = stmt[2]
        start = None
        for i, (test, _) in enumerate(cases):
            if test is not None and _strict_equals(disc, self.eval(test, scope)):
                start = i
                break
        if start is None:
            start = next((i for i, (test, _) in enumerate(cases) if test is None), None)
        if start is None:
            return
        try:
            for _, body in cases[start:]:
                for s in body:
                    self.exec_stmt(s, scope)
        except _Break:
            pass

    # -- expressions -----------------------------------------------------
    def eval(self, node, scope):
        kind = node[0]
        if kind == 'name':
            return scope.lookup(node[1])
        if kind == 'num' or kind == 'str' or kind == 'lit':
            return node[1]
        if kind == 'member':
            obj = self.eval(node[1], scope)
            return self.get_member(obj, self.eval(node[2], scope))
        if kind == 'call':
            return self._call(node, scope)
        if kind == 'binary':
            return self.binary(node[1], self.eval(node[2], scope), self.eval(node[3], scope))
        if kind == 'assign':
            return self._assign(node, scope)
        if kind == 'update':
            return self._update(node, scope)
        if kind == 'logical':
            left = self.eval(node[2], scope)
            op = node[1]
            if op == '&&':
                return self.eval(node[3], scope) if _truthy(left) else left
            if op == '||':
                return left if _truthy(left) else self.eval(node[3], scope)
            return self.eval(node[3], scope) if left is None or left is UNDEFINED else left
        if kind == 'cond':
            return self.eval(node[2] if _truthy(self.eval(node[1], scope)) else node[3], scope)
        if kind == 'unary':
            return self._unary(node, scope)
        if kind == 'array':
            return [self.eval(e, scope) for e in node[1]]
        if kind == 'object':
            return dict((k, self.eval(v, scope)) for k, v in node[1])
        if kind == 'func':
            _, name, params, body, arrow, expr_body = node
            return JSFunction(self, name, params, body, scope, arrow, expr_body)
        if kind == 'regex':
            return JSRegExp(node[1], node[2])
        if kind == 'seq':
            value = UNDEFINED
            for e in node[1]:
                value = self.eval(e, scope)
            return value
        if kind == 'new':
            return self._new(node, scope)
        raise JSError('unsupported expression %s' % kind)

    def _call(self, node, scope):
        callee = node[1]
        if callee[0] == 'member':
            this = self.eval(callee[1], scope)
            fn = self.get_member(this, self.eval(callee[2], scope))
        else:
            this = UNDEFINED
            fn = self.eval(callee, scope)
        args = [self.eval(a, scope) for a in node[2]]
        return self.call(fn, this, args)

    def call(self, fn, this, args):
        if isinstance(fn, (JSFunction, Native)):
            self._tick()
            return fn.call(this, args)
        raise JSError('%s is not a function' % _to_string(fn))

    def _new(self, node, scope):
        ctor = self.eval(node[1], scope)
        args = [self.eval(a, scope) for a in node[2]]
        if ctor is self._array_ctor:
            if len(args) == 1 and _is_num(args[0]):
                return [UNDEFINED] * int(args[0])
            return list(args)
        if ctor is self._object_ctor:
            return {}
        if isinstance(ctor, JSFunction):
            obj = {}
            result = ctor.call(obj, args)
            return result if isinstance(result, (dict, list)) else obj
        raise JSError('unsupported constructor')

    def _unary(self, node, scope):
        op = node[1]
        if op == 'typeof':
            if node[2][0] == 'name':
                try:
                    return _typeof(scope.lookup(node[2][1]))
                except JSError:
                    return 'undefined'
            return _typeof(self.eval(node[2], scope))
        value = self.eval(node[2], scope)
        if op == '!':
            return not _truthy(value)
        if op == '-':
            return _norm(-_to_number(value))
        if op == '+':
            return _to_number(value)
        if op == '~':
            return ~_to_int32(value)
        if op == 'void':
            return UNDEFINED
        if op == 'delete':
            return True
        raise JSError('unsupported unary %s' % op)

    def _assign(self, node, scope):
        op, target = node[1], node[2]
        if target[0] == 'name':
            if op == '=':
                value = self.eval(node[3], scope)
            else:
                value = self.binary(op[:-1], scope.lookup(target[1]), self.eval(node[3], scope))
            scope.assign(target[1], value)
            return value
        obj = self.eval(target[1], scope)
        key = self.eval(target[2], scope)
        if op == '=':
            value = self.eval(node[3], scope)
        else:
            value = self.binary(op[:-1], self.get_member(obj, key), self.eval(node[3], scope))
        self.set_member(obj, key, value)
        return value

    def _update(self, node, scope):
        op, prefix, target = node[1], node[2], node[3]
        delta = 1 if op == '++' else -1
        if target[0] == 'name':
            old = _to_number(scope.lookup(target[1]))
            new = _norm(old + delta)
            scope.assign(target[1], new)
        elif target[0] == 'member':
            obj = self.eval(target[1], scope)
            key = self.eval(target[2], scope)
            old = _to_number(self.get_member(obj, key))
            new = _norm(old + delta)
            self.set_member(obj, key, new)
        else:
            raise JSError('invalid update target')
        return new if prefix else old

    def binary(self, op, a, b):
        if op == '+':
            a, b = _to_primitive(a), _to_primitive(b)
            if isinstance(a, str_types) or isinstance(b, str_types):
                return _to_string(a) + _to_string(b)
            return _norm(_to_number(a) + _to_number(b))
        if op == '-':
            return _norm(_to_number(a) - _to_number(b))
        if op == '*':
            return _norm(_to_number(a) * _to_number(b))
        if op == '/':
            x, y = _to_number(a), _to_number(b)
            if y == 0:
                if x == 0 or x != x:
                    return _NAN
                return _INF if (x > 0) == (math.copysign(1, y) > 0) else -_INF
            return _norm(float(x) / y)
        if op == '%':
            x, y = _to_number(a), _to_number(b)
            if y == 0 or x != x or y != y or x in (_INF, -_INF):
                return _NAN
            if isinstance(x, int) and isinstance(y, int):
                r = abs(x) % abs(y)
                return -r if x < 0 else r
            return _norm(math.fmod(x, y))
        if op == '**':
            return _norm(_to_number(a) ** _to_number(b))
        if op == '&':
            return _to_int32(a) & _to_int32(b)
        if op == '|':
            return _to_int32(_to_int32(a) | _to_int32(b))
        if op == '^':
            return _to_int32(_to_int32(a) ^ _to_int32(b))
        if op == '<<':
            return _to_int32(_to_int32(a) << (_to_uint32(b) & 31))
        if op == '>>':
            return _to_int32(a) >> (_to_uint32(b) & 31)
        if op == '>>>':
            return _to_uint32(a) >> (_to_uint32(b) & 31)
        if op == '===':
            return _strict_equals(a, b)
        if op == '!==':
            return not _strict_equals(a, b)
        if op == '==':
            return _loose_equals(a, b)
        if op == '!=':
            return not _loose_equals(a, b)
        if op in ('<', '>', '<=', '>='):
            a, b = _to_primitive(a), _to_primitive(b)
            if not (isinstance(a, str_types) and isinstance(b, str_types)):
                a, b = _to_number(a), _to_number(b)
                if a != a or b != b:
                    return False
            if op == '<':
                return a < b
            if op == '>':
                return a > b
            if op == '<=':
                return a <= b
            return a >= b
        if op == 'in':
            if isinstance(b, dict):
                return _to_string(a) in b
            if isinstance(b, list):
                idx = _to_number(a)
                return _is_num(idx) and 0 <= idx < len(b)
            raise JSError("cannot use 'in' operator")
        if op == 'instanceof':
            return (isinstance(a, list) and b is self._array_ctor) or \
                   (isinstance(a, dict) and b is self._object_ctor)
        raise JSError('unsupported operator %s' % op)

    # -- member access ---------------------------------------------------
    def get_member(self, obj, key):
        if isinstance(obj, str_types):
            if key == 'length':
                return len(obj)
            idx = _index(key)
            if idx is not None:
                return obj[idx] if 0 <= idx < len(obj) else UNDEFINED
            method = _STRING_METHODS.get(_to_string(key))
            if method:
                return Native(lambda this, args, m=method: m(self, this, args), key)
            return UNDEFINED
        if isinstance(obj, list):
            if key == 'length':
                return len(obj)
            idx = _index(key)
            if idx is not None:
                return obj[idx] if 0 <= idx < len(obj) else UNDEFINED
            method = _ARRAY_METHODS.get(_to_string(key))
            if method:
                return Native(lambda this, args, m=method: m(self, this, args), key)
            return UNDEFINED
        if isinstance(obj, dict):
            return obj.get(_to_string(key), UNDEFINED)
        if isinstance(obj, bool):
            return Native(lambda this, args: _to_string(this), 'toString') if key == 'toString' else UNDEFINED
        if _is_num(obj):
            if key == 'toString':
                return Native(lambda this, args: _num_to_string(
                    this, 10 if not args or args[0] is UNDEFINED else _to_int(args[0])), 'toString')
            if key == 'toFixed':
                return Native(lambda this, args: '%.*f' % (_to_int(args[0]) if args else 0, this), 'toFixed')
            return UNDEFINED
        if isinstance(obj, JSRegExp):
            if key == 'test':
                return Native(lambda this, args: this.regex.search(_to_string(args[0] if args else UNDEFINED)) is not None, 'test')
            if key == 'source':
                return obj.source
            if key == 'global':
                return obj.is_global
            if key == 'lastIndex':
                return obj.last_index
            return UNDEFINED
        if isinstance(obj, (JSFunction, Native)):
            if key == 'call':
                return Native(lambda this, args: self.call(obj, args[0] if args else UNDEFINED, args[1:]), 'call')
            if key == 'apply':
                return Native(lambda this, args: self.call(
                    obj, args[0] if args else UNDEFINED,
                    list(args[1]) if len(args) > 1 and isinstance(args[1], list) else []), 'apply')
            if key == 'name':
                return obj.name or ''
            if key == 'length':
                return len(obj.params) if isinstance(obj, JSFunction) else 0
            statics = getattr(self, '_statics', {}).get(id(obj))
            if statics and _to_string(key) in statics:
                return statics[_to_string(key)]
            return UNDEFINED
        raise JSError('Cannot read properties of %s (reading %r)' % (_to_string(obj), _to_string(key)))

    def set_member(self, obj, key, value):
        if isinstance(obj, list):
            if key == 'length':
                n = _to_int(value)
                del obj[n:]
                obj.extend([UNDEFINED] * (n - len(obj)))
                return
            idx = _index(key)
            if idx is None:
                raise JSError('unsupported array property %r' % (key,))
            if idx >= len(obj):
                obj.extend([UNDEFINED] * (idx + 1 - len(obj)))
            obj[idx] = value
            return
        if isinstance(obj, dict):
            obj[_to_string(key)] = value
            return
        if isinstance(obj, JSRegExp) and key == 'lastIndex':
            obj.last_index = _to_int(value)
            return
        raise JSError('Cannot set properties of %s' % _to_string(obj))

    # -- globals ---------------------------------------------------------
    def _install_globals(self):
        g = self.globals.vars

        def native(name, fn):
            return Native(fn, name)

        self._array_ctor = native('Array', lambda this, args: list(args))
        self._object_ctor = native('Object', lambda this, args: {})
        string_ctor = native('String', lambda this, args: _to_string(args[0]) if args else '')
        number_ctor = native('Number', lambda this, args: _to_number(args[0]) if args else 0)
        self._statics = {
            id(string_ctor): {'fromCharCode': native('fromCharCode', lambda this, args: ''.join(
                unichr(_to_uint32(a) & 0xFFFF) for a in args))},
            id(self._array_ctor): {'isArray': native('isArray', lambda this, args: bool(args) and isinstance(args[0], list))},
            id(self._object_ctor): {'keys': native('keys', lambda this, args: list(args[0].keys()) if args and isinstance(args[0], dict) else [])},
            id(number_ctor): {'isNaN': native('isNaN', lambda this, args: _to_number(args[0]) != _to_number(args[0]))},
        }
        g.update({
            'undefined': UNDEFINED,
            'NaN': _NAN,
            'Infinity': _INF,
            'String': string_ctor,
            'Number': number_ctor,
            'Array': self._array_ctor,
            'Object': self._object_ctor,
            'atob': native('atob', lambda this, args: _atob(_to_string(args[0] if args else UNDEFINED))),
            'btoa': native('btoa', lambda this, args: _btoa(_to_string(args[0] if args else UNDEFINED))),
            'parseInt': native('parseInt', lambda this, args: _parse_int(
                _to_string(args[0] if args else UNDEFINED), _to_int(args[1]) if len(args) > 1 else 0)),
            'parseFloat': native('parseFloat', lambda this, args: _parse_float(_to_string(args[0] if args else UNDEFINED))),
            'isNaN': native('isNaN', lambda this, args: _to_number(args[0] if args else UNDEFINED) != _to_number(args[0] if args else UNDEFINED)),
            'escape': native('escape', lambda this, args: _escape(_to_string(args[0]))),
            'unescape': native('unescape', lambda this, args: _unescape(_to_string(args[0]))),
            'decodeURIComponent': native('decodeURIComponent', lambda this, args: _unquote(_to_string(args[0]))),
            'encodeURIComponent': native('encodeURIComponent', lambda this, args: _quote(_to_string(args[0]))),
            'Math': {
                'random': native('random', lambda this, args: random.random()),
                'floor': native('floor', lambda this, args: _norm(float(math.floor(_to_number(args[0]))))),
                'ceil': native('ceil', lambda this, args: _norm(float(math.ceil(_to_number(args[0]))))),
                'round': native('round', lambda this, args: _norm(float(math.floor(_to_number(args[0]) + 0.5)))),
                'abs': native('abs', lambda this, args: abs(_to_number(args[0]))),
                'max': native('max', lambda this, args: max([_to_number(a) for a in args]) if args else -_INF),
                'min': native('min', lambda this, args: min([_to_number(a) for a in args]) if args else _INF),
                'pow': native('pow', lambda this, args: _norm(_to_number(args[0]) ** _to_number(args[1]))),
                'sqrt': native('sqrt', lambda this, args: _norm(math.sqrt(_to_number(args[0])))),
                'PI': math.pi,
            },
        })
        g['window'] = g['self'] = g['globalThis'] = {}


def _index(key):
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key if key >= 0 else None
    if isinstance(key, float):
        return int(key) if key.is_integer() and key >= 0 else None
    if isinstance(key, str_types) and key.isdigit():
        return int(key)
    return None


def _atob(s):
    s = re.sub(r'[\t\n\f\r ]', '', s)
    if len(s) % 4 == 0 and s.endswith('='):
        pass
    elif len(s) % 4 == 1 or not re.match(r'^[A-Za-z0-9+/]*={0,2}$', s):
        raise JSThrow('InvalidCharacterError: atob')
    s = s.rstrip('=')
    s += '=' * (-len(s) % 4)
    try:
        return base64.b64decode(s.encode('ascii')).decode('latin-1')
    except Exception:
        raise JSThrow('InvalidCharacterError: atob')


def _btoa(s):
    try:
        return base64.b64encode(s.encode('latin-1')).decode('ascii')
    except UnicodeEncodeError:
        raise JSThrow('InvalidCharacterError: btoa')


def _parse_int(s, radix=0):
    s = s.strip()
    m = re.match(r'^([+-]?)(0[xX])?', s)
    sign = -1 if m.group(1) == '-' else 1
    s = s[len(m.group(1)):]
    if radix in (0, 16) and m.group(2):
        s = s[2:]
        radix = 16
    radix = radix or 10
    if not 2 <= radix <= 36:
        return _NAN
    digits = _B36[:radix]
    n = 0
    count = 0
    for ch in s.lower():
        d = digits.find(ch)
        if d < 0:
            break
        n = n * radix + d
        count += 1
    return sign * n if count else _NAN


def _parse_float(s):
    m = re.match(r'^\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?', s)
    return _norm(float(m.group(0))) if m else _NAN


try:
    from urllib.parse import quote as _q, unquote as _uq
except ImportError:  # Python 2
    from urllib import quote as _q, unquote as _uq  # noqa: F401


def _quote(s):
    return _q(s.encode('utf-8'), safe="-_.!~*'()")


def _unquote(s):
    return _uq(s)


def _escape(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch.isalnum() and o < 128 or ch in '@*_+-./':
            out.append(ch)
        elif o < 256:
            out.append('%%%02X' % o)
        else:
            out.append('%%u%04X' % o)
    return ''.join(out)


def _unescape(s):
    s = re.sub(r'%u([0-9a-fA-F]{4})', lambda m: unichr(int(m.group(1), 16)), s)
    return re.sub(r'%([0-9a-fA-F]{2})', lambda m: unichr(int(m.group(1), 16)), s)


# -- String.prototype ---------------------------------------------------
def _arg(args, i):
    return args[i] if i < len(args) else UNDEFINED


def _clamp_slice(length, start, end):
    start = _to_int(start, 0)
    end = length if end is UNDEFINED else _to_int(end, length)
    if start < 0:
        start = max(length + start, 0)
    if end < 0:
        end = max(length + end, 0)
    return min(start, length), min(end, length)


def _s_char_code_at(interp, this, args):
    i = _to_int(_arg(args, 0), 0)
    return ord(this[i]) if 0 <= i < len(this) else _NAN


def _s_char_at(interp, this, args):
    i = _to_int(_arg(args, 0), 0)
    return this[i] if 0 <= i < len(this) else ''


def _s_split(interp, this, args):
    sep = _arg(args, 0)
    limit = _arg(args, 1)
    if sep is UNDEFINED:
        parts = [this]
    elif isinstance(sep, JSRegExp):
        parts = sep.regex.split(this)
        parts = [UNDEFINED if p is None else p for p in parts]
    else:
        sep = _to_string(sep)
        parts = list(this) if sep == '' else this.split(sep)
    if limit is not UNDEFINED:
        parts = parts[:_to_int(limit)]
    return parts


def _expand_replacement(template, m):
    def repl(t):
        token = t.group(0)
        if token == '$$':
            return '$'
        if token == '$&':
            return m.group(0)
        idx = int(token[1:])
        if idx <= (m.re.groups or 0):
            return m.group(idx) or ''
        return token
    return re.sub(r'\$\$|\$&|\$\d{1,2}', repl, template)


def _s_replace(interp, this, args):
    pattern, repl = _arg(args, 0), _arg(args, 1)
    if isinstance(pattern, JSRegExp):
        def sub(m):
            if isinstance(repl, (JSFunction, Native)):
                groups = [UNDEFINED if g is None else g for g in m.groups()]
                return _to_string(interp.call(repl, UNDEFINED, [m.group(0)] + groups + [m.start(), this]))
            return _expand_replacement(_to_string(repl), m)
        return pattern.regex.sub(sub, this, count=0 if pattern.is_global else 1)
    needle = _to_string(pattern)
    idx = this.find(needle)
    if idx < 0:
        return this
    if isinstance(repl, (JSFunction, Native)):
        rep = _to_string(interp.call(repl, UNDEFINED, [needle, idx, this]))
    else:
        rep = _to_string(repl).replace('$&', needle).replace('$$', '$')
    return this[:idx] + rep + this[idx + len(needle):]


def _s_substr(interp, this, args):
    start = _to_int(_arg(args, 0), 0)
    if start < 0:
        start = max(len(this) + start, 0)
    length = _arg(args, 1)
    if length is UNDEFINED:
        return this[start:]
    return this[start:start + max(_to_int(length), 0)]


def _s_substring(interp, this, args):
    n = len(this)
    a = min(max(_to_int(_arg(args, 0), 0), 0), n)
    b = n if _arg(args, 1) is UNDEFINED else min(max(_to_int(_arg(args, 1)), 0), n)
    if a > b:
        a, b = b, a
    return this[a:b]


def _s_slice(interp, this, args):
    a, b = _clamp_slice(len(this), _arg(args, 0), _arg(args, 1))
    return this[a:b] if a < b else ''


_STRING_METHODS = {
    'charCodeAt': _s_char_code_at,
    'charAt': _s_char_at,
    'split': _s_split,
    'replace': _s_replace,
    'substr': _s_substr,
    'substring': _s_substring,
    'slice': _s_slice,
    'indexOf': lambda interp, this, args: this.find(_to_string(_arg(args, 0)), _to_int(_arg(args, 1), 0)),
    'lastIndexOf': lambda interp, this, args: this.rfind(_to_string(_arg(args, 0))),
    'toLowerCase': lambda interp, this, args: this.lower(),
    'toUpperCase': lambda interp, this, args: this.upper(),
    'trim': lambda interp, this, args: this.strip(),
    'concat': lambda interp, this, args: this + ''.join(_to_string(a) for a in args),
    'toString': lambda interp, this, args: this,
    'valueOf': lambda interp, this, args: this,
    'repeat': lambda interp, this, args: this * _to_int(_arg(args, 0), 0),
    'startsWith': lambda interp, this, args: this.startswith(_to_string(_arg(args, 0))),
    'endsWith': lambda interp, this, args: this.endswith(_to_string(_arg(args, 0))),
    'includes': lambda interp, this, args: _to_string(_arg(args, 0)) in this,
}


# -- Array.prototype ----------------------------------------------------
def _a_splice(interp, this, args):
    n = len(this)
    start = _to_int(_arg(args, 0), 0)
    start = max(n + start, 0) if start < 0 else min(start, n)
    count = n - start if len(args) < 2 else min(max(_to_int(args[1], 0), 0), n - start)
    removed = this[start:start + count]
    this[start:start + count] = list(args[2:])
    return removed


def _a_slice(interp, this, args):
    a, b = _clamp_slice(len(this), _arg(args, 0), _arg(args, 1))
    return this[a:b] if a < b else []


def _a_join(interp, this, args):
    sep = ',' if _arg(args, 0) is UNDEFINED else _to_string(args[0])
    return sep.join('' if x is None or x is UNDEFINED else _to_string(x) for x in this)


def _a_reverse(interp, this, args):
    this.reverse()
    return this


def _a_push(interp, this, args):
    this.extend(args)
    return len(this)


def _a_map(interp, this, args):
    fn = _arg(args, 0)
    return [interp.call(fn, UNDEFINED, [v, i, this]) for i, v in enumerate(list(this))]


def _a_for_each(interp, this, args):
    fn = _arg(args, 0)
    for i, v in enumerate(list(this)):
        interp.call(fn, UNDEFINED, [v, i, this])
    return UNDEFINED


def _a_filter(interp, this, args):
    fn = _arg(args, 0)
    return [v for i, v in enumerate(list(this)) if _truthy(interp.call(fn, UNDEFINED, [v, i, this]))]


def _a_index_of(interp, this, args):
    needle = _arg(args, 0)
    for i, v in enumerate(this):
        if _strict_equals(v, needle):
            return i
    return -1


_ARRAY_METHODS = {
    'join': _a_join,
    'reverse': _a_reverse,
    'splice': _a_splice,
    'slice': _a_slice,
    'push': _a_push,
    'pop': lambda interp, this, args: this.pop() if this else UNDEFINED,
    'shift': lambda interp, this, args: this.pop(0) if this else UNDEFINED,
    'unshift': lambda interp, this, args: (this.__setitem__(slice(0, 0), list(args)), len(this))[1],
    'concat': lambda interp, this, args: this + [x for a in args for x in (a if isinstance(a, list) else [a])],
    'indexOf': _a_index_of,
    'map': _a_map,
    'forEach': _a_for_each,
    'filter': _a_filter,
    'toString': _a_join,
}
