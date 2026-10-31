"""A small stdlib-only xterm screen model, enough to read Claude Code (Ink) and Codex (ratatui) TUIs.

Feed it the bytes a program writes to its pty; read back the visible rows with `lines()`.
Only the faint/dim attribute (SGR 2) is tracked, because dim text is placeholder or suggestion
text in the composer and must not be mistaken for something the user typed.

    s = Screen(120, 40)
    s.feed(chunk)                # bytes or str, any split point
    s.lines(dim=False)           # rows with dim cells blanked
    s.cursor, s.alt              # (row, col), alternate screen active

Cells hold one string each: a character (plus any combining marks), ' ' for blank, and '' for
the right half of a double-width character. Unknown or unsupported sequences are parsed to their
end and ignored, so they never desynchronise the parser.
"""
import codecs
import re
import unicodedata

GROUND, ESC, ESC_INT, CSI, OSC, STR, STR_ESC = range(7)
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f]")
_CSI_SEQ = re.compile("([\x20-\x3f]*)([\x40-\x7e])")
_OSC_END = re.compile("[\x07\x1b\x9c\x18\x1a]")
_STR_END = re.compile("[\x1b\x9c\x18\x1a]")
_DEC_GRAPHICS = str.maketrans({
    "`": "◆", "a": "▒", "f": "°", "g": "±", "j": "┘", "k": "┐", "l": "┌", "m": "└", "n": "┼",
    "o": "⎺", "p": "⎻", "q": "─", "r": "⎼", "s": "⎽", "t": "├", "u": "┤", "v": "┴", "w": "┬",
    "x": "│", "y": "≤", "z": "≥", "{": "π", "|": "≠", "}": "£", "~": "·"})
_widths = {}


def char_width(c):
    """Terminal cell width of one code point: 0 (combining/format), 1, or 2 (East Asian W/F)."""
    w = _widths.get(c)
    if w is None:
        if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf"):
            w = 0
        else:
            w = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        _widths[c] = w
    return w


class Screen:
    def __init__(self, cols=120, rows=40):
        self.cols, self.rows = max(1, int(cols)), max(1, int(rows))
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._state, self._seq = GROUND, ""
        self.title = ""  # last window title (OSC 0/2); the built-in agent reports busy/idle through it
        self._osc = ""
        self._reset()

    # ----------------------------------------------------------------- public API
    @property
    def cursor(self):
        return (self.y, self.x)

    def feed(self, data):
        if isinstance(data, (bytes, bytearray, memoryview)):
            data = self._decoder.decode(bytes(data))
        if data:
            self._parse(data)

    def lines(self, dim=True):
        out = []
        for row, att in zip(self.buf, self.att):
            if dim or not any(att):
                out.append("".join(row).rstrip())
            else:
                out.append("".join(" " if a else c for c, a in zip(row, att)).rstrip())
        return out

    def resize(self, cols, rows):
        """Resize both screens. Content stays anchored top-left; when the screen gets shorter and
        the cursor row would fall off the bottom, lines are dropped from the top instead (xterm)."""
        cols, rows = max(1, int(cols)), max(1, int(rows))
        for alt in (False, True):
            buf, att = self._screens[alt]
            if alt == self.alt:
                cy = self.y
            else:
                cy = self._alt_saved[1] if (not alt and self._alt_saved) else 0
            drop = max(0, cy - rows + 1) if rows < len(buf) else 0
            buf, att = buf[drop:drop + rows], att[drop:drop + rows]
            while len(buf) < rows:
                buf.append([" "] * cols)
                att.append([False] * cols)
            for r, a in zip(buf, att):
                if cols < len(r):
                    if r[cols] == "":
                        r[cols - 1] = " "
                    del r[cols:], a[cols:]
                else:
                    r.extend([" "] * (cols - len(r)))
                    a.extend([False] * (cols - len(a)))
            self._screens[alt] = (buf, att)
            if alt == self.alt:
                self.y -= drop
            elif drop and not alt and self._alt_saved:
                s = self._alt_saved
                self._alt_saved = (s[0], s[1] - drop) + s[2:]
        self.cols, self.rows = cols, rows
        self.buf, self.att = self._screens[self.alt]
        self.top, self.bot = 0, rows - 1
        self.x, self.y = min(self.x, cols - 1), max(0, min(self.y, rows - 1))
        self.wrap = False

    # ----------------------------------------------------------------- state helpers
    def _blank(self):
        return [[" "] * self.cols for _ in range(self.rows)], [[False] * self.cols for _ in range(self.rows)]

    def _reset(self):
        self._screens = {False: self._blank(), True: self._blank()}
        self.alt = False
        self.buf, self.att = self._screens[False]
        self.x = self.y = 0
        self.wrap, self.dim, self.g0, self.autowrap = False, False, False, True
        self.top, self.bot = 0, self.rows - 1
        self._saved = {False: None, True: None}
        self._alt_saved = None

    def _save(self):
        return (self.x, self.y, self.dim, self.g0)

    def _restore(self, s):
        if s is None:
            s = (0, 0, False, False)
        self.x, self.y = min(s[0], self.cols - 1), min(s[1], self.rows - 1)
        self.dim, self.g0, self.wrap = s[2], s[3], False

    def _set_alt(self, on, clear=False, save=False):
        if on == self.alt:
            return
        if on and save:
            self._alt_saved = self._save()
        if not on and clear:
            self._fill(0, self.rows)
        self.alt = on
        self.buf, self.att = self._screens[on]
        if on and clear:
            self._fill(0, self.rows)
        if not on and save:
            self._restore(self._alt_saved)

    def _fill(self, a, b):
        for y in range(a, b):
            self.buf[y] = [" "] * self.cols
            self.att[y] = [False] * self.cols

    def _split(self, y, x):
        """Make column x a cell boundary: break any wide character straddling it."""
        row = self.buf[y]
        if 0 < x < self.cols and row[x] == "":
            row[x - 1] = row[x] = " "

    def _clear(self, y, a, b):
        a, b = max(0, a), min(self.cols, b)
        if a >= b:
            return
        self._split(y, a)
        self._split(y, b)
        self.buf[y][a:b] = [" "] * (b - a)
        self.att[y][a:b] = [False] * (b - a)

    def _scroll(self, n, top=None, bot=None):
        """Scroll rows top..bot (default: the scroll region) up by n, or down by -n."""
        top = self.top if top is None else top
        bot = self.bot if bot is None else bot
        k = min(abs(n), bot - top + 1)
        if k <= 0:
            return
        for grid, fill in ((self.buf, " "), (self.att, False)):
            blank = [[fill] * self.cols for _ in range(k)]
            region = grid[top:bot + 1]
            grid[top:bot + 1] = region[k:] + blank if n > 0 else blank + region[:-k]

    def _index(self):
        if self.y == self.bot:
            self._scroll(1)
        elif self.y < self.rows - 1:
            self.y += 1
        self.wrap = False

    def _rindex(self):
        if self.y == self.top:
            self._scroll(-1)
        elif self.y > 0:
            self.y -= 1
        self.wrap = False

    # ----------------------------------------------------------------- printing
    def _print(self, run):
        if self.g0:
            run = run.translate(_DEC_GRAPHICS)
        if not (run.isascii() and self.autowrap):
            for c in run:
                self._put(c, char_width(c))
            return
        i, n, cols = 0, len(run), self.cols
        while i < n:
            if self.wrap:
                self.x = 0
                self._index()
            x, y = self.x, self.y
            k = min(n - i, cols - x)
            self._split(y, x)
            self._split(y, x + k)
            self.buf[y][x:x + k] = run[i:i + k]
            self.att[y][x:x + k] = [self.dim] * k
            i += k
            if x + k >= cols:
                self.x, self.wrap = cols - 1, True
            else:
                self.x = x + k

    def _put(self, c, w):
        if w == 0:  # combining mark / zero-width: attach to the previous cell
            x = self.x if self.wrap else self.x - 1
            if x >= 0:
                row = self.buf[self.y]
                if row[x] == "" and x > 0:
                    x -= 1
                row[x] += c
            return
        if w > self.cols:
            w = 1
        if self.wrap and self.autowrap:
            self.x = 0
            self._index()
        if w == 2 and self.x == self.cols - 1:
            if not self.autowrap:
                return
            self._clear(self.y, self.x, self.cols)
            self.x = 0
            self._index()
        x, y = self.x, self.y
        row, att = self.buf[y], self.att[y]
        self._split(y, x)
        self._split(y, x + w)
        row[x], att[x] = c, self.dim
        if w == 2:
            row[x + 1], att[x + 1] = "", self.dim
        if x + w >= self.cols:
            self.x, self.wrap = self.cols - 1, self.autowrap
        else:
            self.x, self.wrap = x + w, False

    # ----------------------------------------------------------------- parser
    def _parse(self, s):
        i, n = 0, len(s)
        while i < n:
            st = self._state
            if st == GROUND:
                m = _CTRL.search(s, i)
                j = m.start() if m else n
                if j > i:
                    self._print(s[i:j])
                    i = j
                    continue
                i += 1
                self._control(s[j])
            elif st == CSI:
                m = _CSI_SEQ.match(s, i)
                if m:
                    self._state = GROUND
                    self._csi(self._seq + m.group(1), m.group(2))
                    i = m.end()
                    continue
                c = s[i]
                o = ord(c)
                if 0x20 <= o <= 0x3f:  # sequence continues in the next chunk
                    self._seq += c
                elif o >= 0x80:  # garbage inside CSI: abort, reprocess as text
                    self._state = GROUND
                    continue
                elif o != 0x7f:
                    self._control(c)  # C0 inside CSI executes; ESC/CAN/SUB change state
                i += 1
            elif st == OSC or st == STR:
                m = (_OSC_END if st == OSC else _STR_END).search(s, i)
                if st == OSC:
                    self._osc = (self._osc + s[i:m.start() if m else len(s)])[-512:]
                if not m:
                    return
                if st == OSC:
                    if self._osc[:2] in ("0;", "2;"):
                        self.title = self._osc[2:]
                    self._osc = ""
                i = m.end()
                self._state = STR_ESC if m.group() == "\x1b" else GROUND
            elif st == STR_ESC:
                if s[i] == "\\":
                    i += 1
                    self._state = GROUND
                else:
                    self._state = ESC  # ESC + anything else aborts the string and starts a new escape
            elif st == ESC:
                i += 1
                self._esc(s[i - 1])
            else:  # ESC_INT
                c = s[i]
                i += 1
                if "\x20" <= c <= "\x2f":
                    self._seq += c
                elif "\x30" <= c <= "\x7e":
                    self._state = GROUND
                    if self._seq == "(":
                        self.g0 = c == "0"
                elif c < " ":
                    self._control(c)
                else:
                    self._state = GROUND

    def _control(self, c):
        if c == "\r":
            self.x, self.wrap = 0, False
        elif c in "\n\x0b\x0c":
            self._index()
        elif c == "\x1b":
            self._state = ESC
        elif c == "\x08":
            self.x, self.wrap = max(0, self.x - 1), False
        elif c == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
        elif c in "\x18\x1a":
            self._state = GROUND
        elif c == "\x9b":
            self._state, self._seq = CSI, ""
        elif c == "\x9d":
            self._state, self._osc = OSC, ""
        elif c in "\x90\x98\x9e\x9f":
            self._state = STR
        elif c == "\x84":
            self._index()
        elif c == "\x85":
            self.x = 0
            self._index()
        elif c == "\x8d":
            self._rindex()
        # BEL, SO/SI, DEL, other C0/C1: ignored

    def _esc(self, c):
        self._state = GROUND
        if c == "[":
            self._state, self._seq = CSI, ""
        elif c == "]":
            self._state, self._osc = OSC, ""
        elif c in "P_^X":
            self._state = STR
        elif "\x20" <= c <= "\x2f":
            self._state, self._seq = ESC_INT, c
        elif c < " ":
            self._state = ESC
            self._control(c)  # executes; ESC restarts, CAN/SUB cancel
        elif c == "7":
            self._saved[self.alt] = self._save()
        elif c == "8":
            self._restore(self._saved[self.alt])
        elif c == "M":
            self._rindex()
        elif c == "D":
            self._index()
        elif c == "E":
            self.x = 0
            self._index()
        elif c == "c":
            self._reset()
        # '=', '>', 'H' (tab set), '\\' (stray ST) and anything else: ignored

    def _sgr(self, params):
        ps = params.split(";")
        i = 0
        while i < len(ps):
            p = ps[i]
            i += 1
            if ":" in p:  # colon sub-parameters (38:2::r:g:b, 4:3) are self-contained
                continue
            if p and not p.isdigit():
                continue
            v = int(p) if p else 0
            if v == 0 or v == 22:
                self.dim = False
            elif v == 2:
                self.dim = True
            elif v in (38, 48, 58) and i < len(ps):
                i += {"5": 2, "2": 4}.get(ps[i], 0)

    def _csi(self, params, f):
        priv = ""
        if params and params[0] in "<=>?":
            priv, params = params[0], params[1:]
        if params and params[-1] < "0":  # intermediate bytes (CSI ! p, CSI SP q, ...): ignore
            return
        if f == "m":
            if not priv:
                self._sgr(params)
            return
        args = []
        for p in params.split(";") if params else ():
            p = p.split(":")[0]
            args.append(int(p) if p.isdigit() else 0)
        if priv:
            if priv == "?" and f in "hl":
                for p in args:
                    self._mode(p, f == "h")
            return

        def a(k=0, d=1):
            return (args[k] if k < len(args) else 0) or d

        cols, rows, y = self.cols, self.rows, self.y
        if f in "ABCDEFGHf`d":
            self.wrap = False
            if f == "A" or f == "F":
                self.y = max(self.top if y >= self.top else 0, y - a())
            elif f == "B" or f == "E":
                self.y = min(self.bot if y <= self.bot else rows - 1, y + a())
            elif f == "C":
                self.x = min(cols - 1, self.x + a())
            elif f == "D":
                self.x = max(0, self.x - a())
            elif f in "G`":
                self.x = min(cols - 1, a() - 1)
            elif f in "Hf":
                self.y, self.x = min(rows - 1, a(0) - 1), min(cols - 1, a(1) - 1)
            elif f == "d":
                self.y = min(rows - 1, a() - 1)
            if f in "EF":
                self.x = 0
        elif f == "J":
            mode = args[0] if args else 0
            self.wrap = False
            if mode == 0:
                self._clear(y, self.x, cols)
                self._fill(y + 1, rows)
            elif mode == 1:
                self._fill(0, y)
                self._clear(y, 0, self.x + 1)
            elif mode == 2:
                self._fill(0, rows)
            # mode 3 erases scrollback only, which this model does not keep
        elif f == "K":
            mode = args[0] if args else 0
            self.wrap = False
            a0, b0 = {0: (self.x, cols), 1: (0, self.x + 1), 2: (0, cols)}.get(mode, (0, 0))
            self._clear(y, a0, b0)
        elif f in "LM":
            if self.top <= y <= self.bot:
                self._scroll(-a() if f == "L" else a(), y, self.bot)
                self.x, self.wrap = 0, False
        elif f in "@PX":
            x, n = self.x, min(a(), cols - self.x)
            row, att = self.buf[y], self.att[y]
            self.wrap = False
            if f == "X":
                self._clear(y, x, x + n)
            elif f == "@":
                self._split(y, x)
                self._split(y, cols - n)
                row[x:x] = [" "] * n
                att[x:x] = [False] * n
                del row[cols:], att[cols:]
            else:
                self._split(y, x)
                self._split(y, x + n)
                del row[x:x + n], att[x:x + n]
                row.extend([" "] * n)
                att.extend([False] * n)
        elif f == "S":
            self._scroll(a())
        elif f == "T":
            if len(args) <= 1:  # 5-parameter CSI T is mouse highlight tracking
                self._scroll(-a())
        elif f == "r":
            top, bot = a(0) - 1, min(rows, a(1, rows)) - 1
            if top < bot:
                self.top, self.bot = top, bot
                self.x, self.y, self.wrap = 0, 0, False
        elif f == "s":
            self._saved[self.alt] = self._save()
        elif f == "u":
            self._restore(self._saved[self.alt])
        # n (DSR), c (DA), t (window ops), h/l (ANSI modes), q, ...: ignored

    def _mode(self, p, on):
        if p == 1049:
            self._set_alt(on, clear=True, save=True)
        elif p == 1047:
            self._set_alt(on, clear=not on)
        elif p == 47:
            self._set_alt(on)
        elif p == 7:
            self.autowrap = on
        # 1, 25, 1000-1006, 2004, 2026, ...: ignored
