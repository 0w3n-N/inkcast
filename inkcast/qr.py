"""A small QR Code encoder (byte mode, error correction level M). No dependencies.

Inkcast only needs to encode its pairing link, so this covers what that needs:
UTF-8 byte mode, versions 1-40, ECC level M, and the standard mask selection.
It follows ISO/IEC 18004 in the same way as Project Nayuki's reference
implementation (https://www.nayuki.io/page/qr-code-generator-library).

Running in-process also means the link, which contains the pairing key, is
never passed to another program where other users could read it.
"""

from __future__ import annotations

# Level M: error correction codewords per block, and number of blocks, by version.
_ECC_PER_BLOCK = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                  26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28]
_NUM_BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
               17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49]
_ECL_FORMAT_BITS = 0  # level M (L=1, M=0, Q=3, H=2)


def _raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _bit(x, i):
    return (x >> i) & 1 != 0


class _Symbol:
    def __init__(self, ver, data):
        self.ver = ver
        self.size = ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()
        self._codewords(self._interleave(data))
        best, best_penalty = 0, None
        for mask in range(8):
            self._mask(mask)
            self._format(mask)
            penalty = self._penalty()
            if best_penalty is None or penalty < best_penalty:
                best, best_penalty = mask, penalty
            self._mask(mask)  # undo (XOR)
        self._mask(best)
        self._format(best)

    # --- function patterns
    def _set(self, x, y, dark):
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def _function_patterns(self):
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < n and 0 <= yy < n:
                        self._set(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._alignment_positions()
        last = len(pos) - 1
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self._format(0)  # placeholder, rewritten after masking
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self._set(a, b, _bit(bits, i))
                self._set(b, a, _bit(bits, i))

    def _alignment_positions(self):
        if self.ver == 1:
            return []
        count = self.ver // 7 + 2
        step = (self.ver * 8 + count * 3 + 5) // (count * 4 - 4) * 2
        return list(reversed([self.size - 7 - i * step for i in range(count - 1)] + [6]))

    def _format(self, mask):
        data = _ECL_FORMAT_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        n = self.size
        for i in range(6):
            self._set(8, i, _bit(bits, i))
        self._set(8, 7, _bit(bits, 6))
        self._set(8, 8, _bit(bits, 7))
        self._set(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set(14 - i, 8, _bit(bits, i))
        for i in range(8):
            self._set(n - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, _bit(bits, i))
        self._set(8, n - 8, True)  # always-dark module

    # --- data
    def _interleave(self, data):
        ver = self.ver
        blocks_n, ecc_len = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
        raw = _raw_modules(ver) // 8
        short_n = blocks_n - raw % blocks_n
        short_len = raw // blocks_n
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(blocks_n):
            dat = data[k:k + short_len - ecc_len + (0 if i < short_n else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if i < short_n:
                dat = dat + [0]  # placeholder so all blocks line up
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != short_len - ecc_len or j >= short_n:
                    out.append(blk[i])
        return out

    def _codewords(self, data):
        n, i, right = self.size, 0, self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    y = (n - 1 - vert) if ((right + 1) & 2) == 0 else vert
                    if not self.fixed[y][x] and i < len(data) * 8:
                        self.dark[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    def _mask(self, mask):
        rules = (
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        )
        rule = rules[mask]
        for y in range(self.size):
            row, fixed = self.dark[y], self.fixed[y]
            for x in range(self.size):
                if not fixed[x] and rule(x, y):
                    row[x] = not row[x]

    # --- mask penalty (ISO/IEC 18004 section 8.8.2)
    def _penalty(self):
        n, result = self.size, 0
        lines = self.dark + [list(col) for col in zip(*self.dark)]
        for line in lines:
            color, run, history = False, 0, [0] * 7
            for cell in line:
                if cell == color:
                    run += 1
                    if run == 5:
                        result += 3
                    elif run > 5:
                        result += 1
                else:
                    self._add_history(run, history)
                    if not color:
                        result += self._finder_like(history) * 40
                    color, run = cell, 1
            if color:
                self._add_history(run, history)
                run = 0
            self._add_history(run + n, history)
            result += self._finder_like(history) * 40
        d = self.dark
        for y in range(n - 1):
            for x in range(n - 1):
                if d[y][x] == d[y][x + 1] == d[y + 1][x] == d[y + 1][x + 1]:
                    result += 3
        dark = sum(row.count(True) for row in d)
        total = n * n
        result += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return result

    def _add_history(self, run, history):
        if history[0] == 0:
            run += self.size  # light border before the first run
        history.pop()
        history.insert(0, run)

    @staticmethod
    def _finder_like(h):
        n = h[1]
        core = n > 0 and h[2] == h[4] == h[5] == n and h[3] == n * 3
        return (1 if core and h[0] >= n * 4 and h[6] >= n else 0) + (1 if core and h[6] >= n * 4 and h[0] >= n else 0)


def encode(text):
    """The QR matrix for `text` as a list of rows of booleans (True = dark), no border."""
    data = text.encode("utf-8")
    for ver in range(1, 41):
        cap = _data_codewords(ver) * 8
        count_bits = 8 if ver <= 9 else 16
        if 4 + count_bits + len(data) * 8 <= cap:
            break
    else:
        raise ValueError("text is too long for a QR code")
    bits = []

    def put(value, n):
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)  # byte mode
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))  # terminator
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    return _Symbol(ver, codewords).dark


def _bordered(text, border):
    m = encode(text)
    n = len(m) + 2 * border
    blank = [False] * n
    return [blank] * border + [[False] * border + row + [False] * border for row in m] + [blank] * border


def matrix(text, border=2):
    """Rows of '0'/'1' including a light border (quiet zone)."""
    return ["".join("1" if c else "0" for c in row) for row in _bordered(text, border)]


def svg(text, border=2):
    rows = _bordered(text, border)
    n = len(rows)
    path = "".join(f"M{x} {y}h1v1h-1z" for y, row in enumerate(rows) for x, c in enumerate(row) if c)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def terminal(text, border=2):
    """The code drawn with half blocks and explicit colors, so it scans on any terminal theme."""
    rows = _bordered(text, border)
    if len(rows) % 2:
        rows = rows + [[False] * len(rows[0])]
    out = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        line = []
        for t, b in zip(top, bottom):
            fg = "30" if t else "97"   # upper half: dark or white
            bg = "40" if b else "107"  # lower half
            line.append(f"\x1b[{fg};{bg}m▀")
        out.append("".join(line) + "\x1b[0m")
    return "\n".join(out) + "\n"
