"""fm img (T-1630): a stdlib PNG codec and a pixel diff, the base for fm shot --diff and the rasteriser.

decode() reads 8-bit grey, grey+alpha, RGB and RGBA, and 1/2/4/8-bit grey and palette, with all five filters, into
RGBA bytes; encode() writes RGB or RGBA with filter 0. zlib and struct only: Pillow is never needed.
ponytail: Adam7 interlacing and 16-bit samples are refused; add them when a real screenshot needs them.
At the caps (MAX_PIXELS, MAX_FILE) the worst case is about 30 s (Paeth on every row, ~0.8 s a megapixel in pure
Python) and under 1 GB at peak (the file, its joined IDAT, the inflated rows and the RGBA out); a screenshot's
megapixel or two takes about a second and tens of MB.
"""
import os
import stat
import struct
import tempfile
import zlib
from typing import NamedTuple

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SIDE = 1 << 15
MAX_PIXELS = 1 << 25  # 33.5 M: an 8K (7680x4320) frame fits; 134 MB of RGBA
MAX_FILE = 256 << 20
CHANGED = (255, 0, 0, 255)  # the diff image's colour for a changed pixel
FADE = bytes(191 + v // 4 for v in range(256))  # the diff image's unchanged pixels: a's, lightened
MODES = {0: "grey", 2: "rgb", 3: "palette", 4: "grey+alpha", 6: "rgba"}
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
KNOWN = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}


class Image(NamedTuple):
    width: int
    height: int
    mode: str  # the file's colour type: grey, grey+alpha, rgb, rgba or palette
    depth: int  # bits per sample in the file
    rgba: bytes  # width * height * 4 bytes, rows top to bottom


def _name(kind):
    """A chunk type for a message: four letters as they are, anything else (ESC [ 2 J) as a repr, never raw."""
    return kind.decode("ascii") if kind.isalpha() else repr(kind)


def _chunks(data):
    if not data.startswith(SIGNATURE):
        raise ValueError("not a PNG (no PNG signature)")
    pos = len(SIGNATURE)
    while True:
        if pos + 12 > len(data):
            raise ValueError("truncated: the file ends before IEND")
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body, end = data[pos + 8:pos + 8 + n], pos + 12 + n
        if end > len(data):
            raise ValueError(f"truncated: the {_name(kind)} chunk runs past the end of the file")
        if struct.unpack(">I", data[end - 4:end])[0] != zlib.crc32(body, zlib.crc32(kind)):
            raise ValueError(f"bad CRC in the {_name(kind)} chunk")
        yield kind, body
        if kind == b"IEND":
            return
        pos = end


def _header(body):
    if len(body) != 13:
        raise ValueError("bad IHDR")
    w, h, depth, colour, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
    depths = {0: (1, 2, 4, 8, 16), 3: (1, 2, 4, 8)}.get(colour, (8, 16))
    if colour not in MODES or depth not in depths or comp or filt:
        raise ValueError(f"bad IHDR: colour type {colour}, depth {depth}")
    if depth == 16:
        raise ValueError("16-bit PNGs aren't read (8-bit and below only)")
    if interlace:
        raise ValueError("interlaced (Adam7) PNGs aren't read")
    if not (w and h):
        raise ValueError(f"bad IHDR: an empty image ({w}x{h})")
    if w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
        raise ValueError(f"too large: {w}x{h} (at most {MAX_SIDE} a side and {MAX_PIXELS} pixels)")
    return w, h, depth, colour


def _unfilter(raw, h, stride, bpp):
    # ponytail: a pure-Python loop per byte for Sub/Average/Paeth rows (~1 s a megapixel); a C path if it bites
    out, prev, pos = bytearray(), bytes(stride), 0
    for _ in range(h):
        kind, line = raw[pos], bytearray(raw[pos + 1:pos + 1 + stride])
        pos += stride + 1
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif kind == 3:
            for i in range(stride):
                line[i] = (line[i] + (((line[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                if i >= bpp:
                    a, b, c = line[i - bpp], prev[i], prev[i - bpp]
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
                else:
                    line[i] = (line[i] + prev[i]) & 255  # a = c = 0: the predictor is b
        elif kind:
            raise ValueError(f"bad filter type {kind}")
        out += line
        prev = line
    return out


def _samples(rows, w, h, stride, depth):
    """Sub-byte rows unpacked to one byte per sample (each row's padding bits dropped)."""
    per = 8 // depth
    table = [bytes((b >> (8 - depth * (k + 1))) & ((1 << depth) - 1) for k in range(per)) for b in range(256)]
    return b"".join(b"".join(map(table.__getitem__, rows[y * stride:(y + 1) * stride]))[:w] for y in range(h))


def decode(data):
    """PNG bytes → Image; ValueError names what's wrong or unsupported."""
    head = plte = trns = None
    idat = []
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            head = _header(body)
        elif head is None:
            raise ValueError("no IHDR before the image data")
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind not in KNOWN and not kind[0] & 32:
            raise ValueError(f"unknown critical chunk {_name(kind)}")
    if head is None:
        raise ValueError("no IHDR")
    w, h, depth, colour = head
    bits = CHANNELS[colour] * depth
    stride, bpp = (w * bits + 7) // 8, max(1, bits // 8)
    need = h * (stride + 1)
    try:  # capped: a bomb stops at what the header allows
        raw = zlib.decompressobj().decompress(b"".join(idat), need)
    except zlib.error as e:
        raise ValueError(f"bad image data: {e}") from None
    if len(raw) < need:
        raise ValueError(f"short image data: {len(raw)} of {need} bytes")
    px = _unfilter(raw, h, stride, bpp)
    if depth < 8:
        px = _samples(px, w, h, stride, depth)
    if colour == 3:
        if not plte or len(plte) % 3:
            raise ValueError("palette image without a valid PLTE")
        entries, top = len(plte) // 3, max(px)
        if top >= entries:  # the spec's error; a diff tool shouldn't paint over a corrupt file
            raise ValueError(f"palette index past PLTE: {top} in a {entries}-colour palette")
        alpha = (trns or b"")[:entries]
        table = [bytes(plte[i * 3:i * 3 + 3]) + bytes([alpha[i] if i < len(alpha) else 255]) for i in range(entries)]
        return Image(w, h, MODES[colour], depth, b"".join(map(table.__getitem__, px)))
    n, out = w * h, bytearray(w * h * 4)
    if colour in (0, 4):
        grey = px[::CHANNELS[colour]]
        scale = 255 // ((1 << depth) - 1)
        for c in range(3):
            out[c::4] = grey.translate(bytes(min(255, v * scale) for v in range(256))) if scale > 1 else grey
        if colour == 4:
            out[3::4] = px[1::2]
        elif trns and len(trns) >= 2:
            key = struct.unpack(">H", trns[:2])[0]
            out[3::4] = grey.translate(bytes(0 if v == key else 255 for v in range(256)))
        else:
            out[3::4] = b"\xff" * n
    else:
        for c in range(CHANNELS[colour]):
            out[c::4] = px[c::CHANNELS[colour]]
        if colour == 2:
            out[3::4] = b"\xff" * n
            key = struct.unpack(">HHH", trns[:6]) if trns and len(trns) >= 6 else (256,)
            if max(key) < 256:  # ponytail: a per-pixel loop; RGB colour keys are rare
                key = bytes(key)
                for i in range(n):
                    if px[i * 3:i * 3 + 3] == key:
                        out[i * 4 + 3] = 0
    return Image(w, h, MODES[colour], depth, bytes(out))


def encode(width, height, pixels, mode="rgba"):
    """RGB or RGBA bytes (rows top to bottom) → PNG bytes, filter 0."""
    k = {"rgb": 3, "rgba": 4}[mode]
    stride = width * k
    if width <= 0 or height <= 0 or len(pixels) != stride * height:
        raise ValueError(f"{len(pixels)} bytes isn't {width}x{height} {mode}")
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(body, zlib.crc32(kind)))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2 if k == 3 else 6, 0, 0, 0)
    return SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _read(path, head=0):
    """The file's bytes (its first head bytes if head); ValueError past MAX_FILE or for a FIFO or device."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)  # a plain open() of a FIFO waits for a writer
    if not stat.S_ISREG(os.fstat(fd).st_mode):  # a FIFO would block on read, a device never end
        os.close(fd)
        raise ValueError("not a regular file")
    with os.fdopen(fd, "rb") as f:
        data = f.read(head or MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError(f"too large a file (over {MAX_FILE >> 20} MB)")
    return data


def info(path):
    """The header's fields, read without decoding the image (so 16-bit and interlaced files are named too)."""
    try:
        kind, body = next(_chunks(_read(path, head=64)))
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None
    if kind != b"IHDR" or len(body) != 13:
        raise ValueError(f"{path}: no IHDR first")
    w, h, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
    return {"path": path, "width": w, "height": h, "mode": MODES.get(colour, f"colour type {colour}"),
            "depth": depth, "interlaced": bool(interlace)}


def load(path):
    """A PNG file → Image; ValueError for a file that isn't a regular one, is too big, or won't decode."""
    try:
        return decode(_read(path))
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None


def save(path, width, height, pixels, mode="rgba"):
    """Write a PNG through a temporary file and a rename: a symlink at path is replaced, never written through, and
    the file is mkstemp's 0600 (a diff of a screenshot may be as private as the screenshot)."""
    data = encode(width, height, pixels, mode)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".fmimg-", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _differ(p, q, tol):
    """Two RGBA pixels differ unless equal, both fully transparent, or within tol on every channel."""
    return p != q and (p[3] or q[3]) and (not tol or max(abs(x - y) for x, y in zip(p, q)) > tol)


def diff(a, b, tol=0, image=False):
    """{changed, total, bbox: [x0, y0, x1, y1) around the changes or None}; image=True adds "image", the RGBA bytes of
    a faded copy of a with each changed pixel in CHANGED, from the same walk. tol: the largest per-channel difference
    that still counts as equal; two pixels with alpha 0 are equal whatever their colour."""
    if (a.width, a.height) != (b.width, b.height):
        raise ValueError(f"sizes differ: {a.width}x{a.height} vs {b.width}x{b.height}")
    w, row, changed, box, out = a.width, a.width * 4, 0, None, None
    if image:
        out = bytearray(a.rgba.translate(FADE))
        out[3::4] = b"\xff" * (w * a.height)
    for y in range(a.height):
        ra, rb = a.rgba[y * row:(y + 1) * row], b.rgba[y * row:(y + 1) * row]
        if ra == rb:  # most rows of a near-identical pair: one C compare
            continue
        xs = [x for x in range(w) if _differ(ra[x * 4:x * 4 + 4], rb[x * 4:x * 4 + 4], tol)]
        if not xs:
            continue
        changed += len(xs)
        box = [xs[0], y, xs[-1] + 1, y + 1] if box is None else \
            [min(box[0], xs[0]), box[1], max(box[2], xs[-1] + 1), y + 1]
        if image:
            for x in xs:
                i = (y * w + x) * 4
                out[i:i + 4] = bytes(CHANGED)
    d = {"changed": changed, "total": w * a.height, "bbox": box}
    if image:
        d["image"] = bytes(out)
    return d


def cmd_img(args):
    import sys
    from sampo import cli
    try:
        if args.action == "info":
            if len(args.paths) != 1:
                print("sampo: img info takes one PATH", file=sys.stderr)
                return 2
            d = info(args.paths[0])
            cli.out(args, d, f"{d['path']}: {d['width']}x{d['height']} {d['mode']} {d['depth']}-bit"
                               + (", interlaced" if d["interlaced"] else ""))
            return 0
        if len(args.paths) != 2:
            print("sampo: img diff takes two paths, A and B", file=sys.stderr)
            return 2
        tol = getattr(args, "tol", 0) or 0
        if not 0 <= tol <= 255:
            print(f"sampo: img diff --tol is 0 to 255, not {tol}", file=sys.stderr)
            return 2
        a, b = (load(p) for p in args.paths)
        if args.out and os.path.exists(args.out) and any(os.path.samefile(args.out, p) for p in args.paths):
            raise ValueError(f"--out {args.out} is one of the images it compares")
    except (OSError, ValueError) as e:
        print(f"sampo: img: {e}", file=sys.stderr)
        return 2
    if (a.width, a.height) != (b.width, b.height):
        cli.out(args, {"changed": None, "sizes": [[a.width, a.height], [b.width, b.height]], "out": None},
                  f"sizes differ: {a.width}x{a.height} vs {b.width}x{b.height}"
                  + ("; no diff image written" if args.out else ""))
        return 1
    d = diff(a, b, tol=tol, image=bool(args.out))
    if args.out:
        try:
            save(args.out, a.width, a.height, d.pop("image"))
        except OSError as e:
            print(f"sampo: img: can't write {args.out}: {e}", file=sys.stderr)
            return 2
    text = "identical" if not d["changed"] else \
        f"{d['changed']} of {d['total']} pixels changed ({100 * d['changed'] / d['total']:.2f}%) in " \
        f"x {d['bbox'][0]}-{d['bbox'][2] - 1}, y {d['bbox'][1]}-{d['bbox'][3] - 1}"
    within = f" (within {tol} a channel)" if tol else ""
    cli.out(args, dict(d, out=args.out, tol=tol), text + within + (f"; diff image: {args.out}" if args.out else ""))
    return 1 if d["changed"] else 0
