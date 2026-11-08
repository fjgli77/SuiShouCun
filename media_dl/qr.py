"""内置二维码生成器（纯标准库，不依赖 qrcode / Pillow / 任何图片组件）。

实现 QR Code 规范的 Byte 模式、版本 1-5、纠错级别 L、Reed-Solomon 纠错、
8 种掩码与格式信息 BCH 编码，直接输出 SVG。
局域网地址最长也就几十字节，版本 1-5（单块、无需交织）完全够用。

对外只有一个函数： `qr_svg(data)`。
"""

from __future__ import annotations

# (总码字, 纠错码字, 数据码字)，纠错级别 L，版本 1-5 均为单块
_VERSIONS: dict[int, tuple[int, int, int]] = {
    1: (26, 7, 19),
    2: (44, 10, 34),
    3: (70, 15, 55),
    4: (100, 20, 80),
    5: (134, 26, 108),
}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(n: int) -> list[int]:
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, coef in enumerate(g):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _GF_EXP[i])
        g = nxt
    return g


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator(ec_len)
    work = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = work[i]
        if coef:
            for j, g in enumerate(gen):
                work[i + j] ^= _gf_mul(g, coef)
    return work[len(data):]


def _bch_format(data: int) -> int:
    """纠错级别(2bit, L=01) + 掩码(3bit) -> 15bit 格式信息。"""
    d = data << 10
    g = 0x537
    while d.bit_length() >= 11:
        d ^= g << (d.bit_length() - 11)
    return ((data << 10) | d) ^ 0x5412


def _mask_fn(pattern: int):
    if pattern == 0:
        return lambda r, c: (r + c) % 2 == 0
    if pattern == 1:
        return lambda r, c: r % 2 == 0
    if pattern == 2:
        return lambda r, c: c % 3 == 0
    if pattern == 3:
        return lambda r, c: (r + c) % 3 == 0
    if pattern == 4:
        return lambda r, c: (r // 2 + c // 3) % 2 == 0
    if pattern == 5:
        return lambda r, c: (r * c) % 2 + (r * c) % 3 == 0
    if pattern == 6:
        return lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0


# --------------------------------------------------------------------------- #
# 矩阵
# --------------------------------------------------------------------------- #
def _blank(size: int) -> list[list[bool | None]]:
    return [[None] * size for _ in range(size)]


def _setup_patterns(m: list[list[bool | None]], version: int) -> None:
    size = len(m)
    # 定位图案 + 分隔符
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                m[r][c] = bool(
                    0 <= dr <= 6 and 0 <= dc <= 6 and (
                        dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                    )
                )
    # 对齐图案：版本 2-5 只有一个，位于 (size-7, size-7)
    if version >= 2:
        cr = cc = size - 7
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                m[cr + dr][cc + dc] = max(abs(dr), abs(dc)) != 1
    # 定时图案
    for i in range(8, size - 8):
        if m[6][i] is None:
            m[6][i] = i % 2 == 0
        if m[i][6] is None:
            m[i][6] = i % 2 == 0
    # 固定黑点
    m[size - 8][8] = True


def _write_format(m: list[list[bool | None]], mask: int) -> None:
    """按规范把 15bit 格式信息写入两处预留区（L 级别 = 0b01）。"""
    size = len(m)
    bits = _bch_format((0b01 << 3) | mask)
    for i in range(15):
        dark = ((bits >> i) & 1) == 1
        if i < 6:
            m[i][8] = dark
        elif i < 8:
            m[i + 1][8] = dark
        else:
            m[size - 15 + i][8] = dark
        if i < 8:
            m[8][size - i - 1] = dark
        elif i < 9:
            m[8][15 - i] = dark
        else:
            m[8][15 - i - 1] = dark
    m[size - 8][8] = True


def _map_data(m: list[list[bool | None]], codewords: list[int], mask: int) -> None:
    size = len(m)
    bits: list[bool] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append(bool((cw >> i) & 1))
    fn = _mask_fn(mask)
    inc = -1
    row = size - 1
    idx = 0
    for col in range(size - 1, 0, -2):
        # 第 6 列是定时图案，跨过它之后所有列对都要左移一列
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if m[row][c] is None:
                    dark = bits[idx] if idx < len(bits) else False
                    idx += 1
                    if fn(row, c):
                        dark = not dark
                    m[row][c] = dark
            row += inc
            if row < 0 or row >= size:
                row -= inc
                inc = -inc
                break


def _penalty(m: list[list[bool | None]]) -> int:
    size = len(m)
    score = 0
    # N1：行/列连续同色
    for line in list(m) + [[m[r][c] for r in range(size)] for c in range(size)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5
    # N2：2x2 同色块
    for r in range(size - 1):
        for c in range(size - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # N3：1:1:3:1:1 定位符相似图案
    pattern_a = [True, False, True, True, True, False, True, False, False, False, False]
    pattern_b = [False, False, False, False, True, False, True, True, True, False, True]
    for r in range(size):
        for c in range(size - 10):
            window = [m[r][c + i] for i in range(11)]
            if window == pattern_a or window == pattern_b:
                score += 40
    for c in range(size):
        for r in range(size - 10):
            window = [m[r + i][c] for i in range(11)]
            if window == pattern_a or window == pattern_b:
                score += 40
    # N4：黑点比例
    dark = sum(1 for row in m for v in row if v)
    score += int(abs(dark * 100 / (size * size) - 50) / 5) * 10
    return score


def _data_codewords(payload: bytes, version: int) -> list[int]:
    _, ec_len, capacity = _VERSIONS[version]
    bits: list[int] = []

    def put(value: int, count: int) -> None:
        for i in range(count - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                    # Byte 模式
    put(len(payload), 8)              # 版本 1-9：字符计数 8bit
    for byte in payload:
        put(byte, 8)
    max_bits = capacity * 8
    for _ in range(min(4, max(0, max_bits - len(bits)))):
        bits.append(0)                # 终止符
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    pad = 0
    while len(words) < capacity:
        words.append(0xEC if pad % 2 == 0 else 0x11)
        pad += 1
    return words + _rs_encode(words, ec_len)


def make_matrix(data: str, mask: int | None = None) -> list[list[bool]]:
    """生成二维码矩阵；mask=None 时自动选惩罚最低的掩码。"""
    payload = data.encode("utf-8")
    version = next(
        (v for v, (_, _, cap) in sorted(_VERSIONS.items()) if len(payload) + 2 <= cap),
        None,
    )
    if version is None:
        raise ValueError("二维码内容过长，请缩短地址")
    codewords = _data_codewords(payload, version)

    best: list[list[bool | None]] | None = None
    best_score = None
    for candidate in ([mask] if mask is not None else range(8)):
        matrix = _blank(17 + 4 * version)
        _setup_patterns(matrix, version)
        # 格式信息必须先占位，否则数据位流会占用这些模块导致整体错位
        _write_format(matrix, candidate)
        _map_data(matrix, codewords, candidate)
        score = _penalty(matrix)
        if best_score is None or score < best_score:
            best_score, best = score, matrix
    assert best is not None
    return [[bool(v) for v in row] for row in best]


def qr_svg(data: str, border: int = 4) -> str:
    """把文本编码为二维码 SVG 字符串（白底黑块，带静默区）。"""
    matrix = make_matrix(data)
    size = len(matrix)
    total = size + border * 2
    path = "".join(
        f"M{c + border} {r + border}h1v1h-1z"
        for r, row in enumerate(matrix)
        for c, dark in enumerate(row)
        if dark
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
        f'<rect width="{total}" height="{total}" fill="#ffffff"/>'
        f'<path d="{path}" fill="#000000"/></svg>'
    )
