#!/usr/bin/env python3
"""XWD -> PNG converter for the webtop container (no ImageMagick / netpbm / numpy).

WHY THIS EXISTS
---------------
The `takeout-webgui` container has `xwd` and Pillow, but **not** ImageMagick,
netpbm or ffmpeg, and Pillow's bundled `XwdImagePlugin` rejects the dump Xvfb
produces (`UnidentifiedImageError`). `xwd -root` is nevertheless the only way to
capture the **whole desktop** rather than just the browser viewport — and the
whole desktop is what you need when something appears outside the page DOM: a
native dialog, the download shelf, a KDE notification, a modal.

Verified 2026-09-19: produces a coherent 2144x1372 PNG with correct colours and
no stride artifacts (confirmed by eye, not by assumption).

USE
---
    docker exec takeout-webgui sh -c 'DISPLAY=:1 xwd -root -silent -out /tmp/d.xwd'
    docker cp <container>:/tmp/tk_xwd.py /tmp/ && docker exec <container> python3 /tmp/tk_xwd.py /tmp/d.xwd /tmp/d.png

XWD (X Window Dump) version 7 layout, all fields big-endian uint32:

    0 header_size   4 file_version   8 pixmap_format  12 pixmap_depth
   16 width        20 height        24 xoffset        28 byte_order
   32 bitmap_unit  36 bit_order     40 bitmap_pad     44 bits_per_pixel
   48 bytes_per_line  52 visual_class
   56 red_mask     60 green_mask    64 blue_mask      68 bits_per_rgb
   72 colormap_entries  76 ncolors   80 window_width   84 window_height
   88 window_x     92 window_y      96 window_bdrwidth

Pixel data follows the header, then `ncolors` 12-byte colour entries.
`byte_order` 0 == LSBFirst (BGR in memory), 1 == MSBFirst (RGB).
"""
from __future__ import annotations

import struct
import sys

from PIL import Image

HEADER = struct.Struct(">25I")


def read_xwd(path: str) -> tuple[Image.Image, dict]:
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) < HEADER.size:
        raise ValueError("file too small to be an XWD dump")

    f = HEADER.unpack_from(raw, 0)
    (header_size, file_version, pixmap_format, depth, width, height, xoffset,
     byte_order, bitmap_unit, bit_order, bitmap_pad, bits_per_pixel,
     bytes_per_line, visual_class, red_mask, green_mask, blue_mask, bits_per_rgb,
     colormap_entries, ncolors, *_rest) = f

    if file_version != 7:
        raise ValueError(f"unsupported XWD version {file_version} (expected 7)")
    # pixmap_format 2 == ZPixmap; 1 == XYBitmap, 0 == XYPixmap (bit planes)
    if pixmap_format != 2:
        raise ValueError(f"pixmap_format {pixmap_format} is not ZPixmap (2)")

    offset = header_size + ncolors * 12
    need = bytes_per_line * height
    if len(raw) < offset + need:
        raise ValueError(
            f"truncated: need {need} pixel bytes at offset {offset}, "
            f"file has {len(raw) - offset}"
        )

    data = raw[offset:offset + need]
    bpp = bits_per_pixel or depth

    # 24/32bpp on x86 Xvfb is BGR in memory when byte_order is LSBFirst.
    if bpp == 32:
        raw_mode = "BGRX" if byte_order == 0 else "RGBX"
        img = Image.frombytes("RGB", (width, height), data, "raw", raw_mode, bytes_per_line, 1)
    elif bpp == 24:
        raw_mode = "BGR" if byte_order == 0 else "RGB"
        img = Image.frombytes("RGB", (width, height), data, "raw", raw_mode, bytes_per_line, 1)
    else:
        raise ValueError(f"unsupported bits_per_pixel {bpp}")

    meta = {
        "version": file_version, "depth": depth, "bpp": bpp,
        "width": width, "height": height, "byte_order": byte_order,
        "ncolors": ncolors, "header_size": header_size,
        "bytes_per_line": bytes_per_line, "visual_class": visual_class,
    }
    return img, meta


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/_root.xwd"
    dst = sys.argv[2] if len(sys.argv) > 2 else "/tmp/_root.png"
    img, meta = read_xwd(src)
    img.save(dst)
    print("  parsed XWD:", {k: meta[k] for k in
                           ("bpp", "width", "height", "byte_order", "ncolors")})
    print(f"  wrote {dst}  ({img.size[0]}x{img.size[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
