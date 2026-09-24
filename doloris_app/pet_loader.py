"""Codex V2 Pet Atlas parser and custom sprite pack loader for Doloris."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    HAS_PIL = False

# Codex V2 Pet Specification
CODEX_V2_COLS = 8
CODEX_V2_ROWS = 11
CODEX_V2_CELL_W = 192
CODEX_V2_CELL_H = 208

# Codex row indices and frame counts
CODEX_STATE_ROWS = {
    "idle": (0, 6),
    "running-right": (1, 8),
    "running-left": (2, 8),
    "waving": (3, 4),
    "jumping": (4, 5),
    "failed": (5, 8),
    "waiting": (6, 6),
    "running": (7, 6),
    "review": (8, 6),
}

# Mapping supervisor states to pet animations
SUPERVISOR_STATE_MAP = {
    "idle": "idle",
    "working": "running",
    "thinking": "waiting",
    "fixing": "failed",
    "success": "review",
    "greeting": "waving",
}


class PetSkin:
    """Represents an animated pet skin with frame sequences per state."""

    def __init__(self, name: str, display_name: str, frames: Dict[str, List[Image.Image]]):
        self.name = name
        self.display_name = display_name
        self.frames = frames  # state -> list of PIL.Image (RGBA)

    def get_frames(self, state: str) -> List[Image.Image]:
        mapped = SUPERVISOR_STATE_MAP.get(state, state)
        if mapped in self.frames and self.frames[mapped]:
            return self.frames[mapped]
        if "idle" in self.frames and self.frames["idle"]:
            return self.frames["idle"]
        # Fallback to any non-empty state
        for f in self.frames.values():
            if f:
                return f
        return []


def slice_codex_v2_atlas(image_path: Path) -> Dict[str, List[Image.Image]]:
    """Slices a 1536x2288 Codex V2 sprite atlas into animation frame lists."""
    img = Image.open(image_path).convert("RGBA")
    w, h = img.size

    # Handle scaled atlases by computing proportional cell size
    cell_w = w // CODEX_V2_COLS
    cell_h = h // CODEX_V2_ROWS

    result: Dict[str, List[Image.Image]] = {}

    for state_name, (row_idx, frame_count) in CODEX_STATE_ROWS.items():
        frames = []
        for col_idx in range(frame_count):
            x1 = col_idx * cell_w
            y1 = row_idx * cell_h
            x2 = x1 + cell_w
            y2 = y1 + cell_h
            cell = img.crop((x1, y1, x2, y2))
            frames.append(cell)
        result[state_name] = frames

    return result


def load_folder_pet(folder: Path) -> Optional[PetSkin]:
    """Loads a custom pet from a directory."""
    manifest_file = folder / "pet.json"
    display_name = folder.name
    spritesheet_file = None

    if manifest_file.exists():
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
                display_name = meta.get("displayName", display_name)
                sheet_name = meta.get("spritesheetPath", "")
                if sheet_name and (folder / sheet_name).exists():
                    spritesheet_file = folder / sheet_name
        except Exception:
            pass

    # Check for direct spritesheet file
    if not spritesheet_file:
        for ext in (".webp", ".png"):
            cand = folder / f"spritesheet{ext}"
            if cand.exists():
                spritesheet_file = cand
                break

    if spritesheet_file:
        try:
            frames = slice_codex_v2_atlas(spritesheet_file)
            return PetSkin(folder.name, display_name, frames)
        except Exception:
            pass

    # Check for state subdirectories (idle/, working/, etc.)
    frames: Dict[str, List[Image.Image]] = {}
    for state in ("idle", "running", "waiting", "failed", "review", "waving"):
        sub = folder / state
        if sub.is_dir():
            imgs = []
            for p in sorted(sub.glob("*.png")):
                try:
                    imgs.append(Image.open(p).convert("RGBA"))
                except Exception:
                    pass
            if imgs:
                frames[state] = imgs

    if frames:
        return PetSkin(folder.name, display_name, frames)

    return None


def create_procedural_doloris_skin() -> PetSkin:
    """Generates the procedural Doloris Rocker Pup skin with PIL vector primitives.

    All states wear the cool black baseball cap with stylish bangs peeking out.
    Working mode features active guitar strumming with pick, animated tilt, and dynamic flying notes.
    Hard alpha thresholding ensures 100% crisp transparency without black borders on Windows.
    """
    import math

    W, H = CODEX_V2_CELL_W, CODEX_V2_CELL_H  # 192, 208
    SCALE = 4  # 4x super-sampling for anti-aliased vector smoothness
    SW, SH = W * SCALE, H * SCALE

    def S(v: float) -> int:
        return int(v * SCALE)

    # Color palette directly matched to reference illustrations
    c_gold = (248, 186, 68, 255)
    c_gold_outline = (212, 146, 42, 255)
    c_ear = (218, 138, 36, 255)
    c_ear_outline = (182, 110, 24, 255)

    c_hair = (244, 218, 204, 255)
    c_hair_shade = (220, 188, 172, 255)

    c_cloth = (224, 200, 190, 255)
    c_cloth_shade = (195, 170, 160, 255)
    c_skirt = (205, 175, 165, 255)
    c_skirt_stripe = (235, 215, 208, 255)
    c_collar_white = (252, 252, 255, 255)
    c_red_ribbon = (205, 48, 56, 255)
    c_ribbon_knot = (235, 65, 75, 255)

    c_cap = (38, 40, 44, 255)
    c_cap_shade = (20, 21, 23, 255)
    c_cap_light = (78, 80, 88, 255)
    c_visor = (26, 27, 30, 255)

    c_guitar = (36, 36, 40, 255)
    c_pickguard = (246, 204, 62, 255)
    c_knob = (225, 180, 45, 255)
    c_neck = (238, 226, 212, 255)
    c_fret = (140, 135, 130, 255)
    c_eye = (35, 25, 20, 255)

    def draw_character_frame(
        bob_y: int = 0,
        rot: float = 0.0,
        eye_state: str = "open",
        strum_offset: int = 0,
        fret_slide: float = 0.0,
        extra_decor=None,
    ) -> Image.Image:
        canvas = Image.new("RGBA", (SW, SH), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        cx = SW // 2
        cy = int((118 + bob_y) * SCALE)
        head_y = cy - int(38 * SCALE)

        # 1. Back hair (PLACED HIGHER UP behind shoulders)
        draw.ellipse([cx - S(44), head_y + S(6), cx - S(22), head_y + S(38)], fill=c_hair, outline=c_hair_shade, width=S(1.2))
        draw.ellipse([cx - S(38), head_y + S(14), cx - S(18), head_y + S(42)], fill=c_hair)
        draw.ellipse([cx + S(22), head_y + S(6), cx + S(44), head_y + S(38)], fill=c_hair, outline=c_hair_shade, width=S(1.2))
        draw.ellipse([cx + S(18), head_y + S(14), cx + S(38), head_y + S(42)], fill=c_hair)

        # 2. Golden Retriever Drooping Ears (further shrunk and cute)
        draw.ellipse([cx - S(44), head_y - S(5), cx - S(26), head_y + S(32)], fill=c_ear, outline=c_ear_outline, width=S(1.8))
        draw.ellipse([cx + S(26), head_y - S(5), cx + S(44), head_y + S(32)], fill=c_ear, outline=c_ear_outline, width=S(1.8))

        # 3. Lower Body & Sailor outfit
        draw.ellipse([cx - S(32), cy + S(36), cx + S(32), cy + S(78)], fill=c_gold, outline=c_gold_outline, width=S(1.8))

        # Skirt
        skirt_poly = [
            (cx - S(35), cy + S(24)),
            (cx + S(35), cy + S(24)),
            (cx + S(44), cy + S(58)),
            (cx - S(44), cy + S(58)),
        ]
        draw.polygon(skirt_poly, fill=c_skirt, outline=c_cloth_shade, width=S(1.5))
        for off in [-30, -18, -6, 6, 18, 30]:
            draw.line([(cx + S(off), cy + S(24)), (cx + S(off * 1.18), cy + S(58))], fill=c_cloth_shade, width=S(1.2))
        draw.line([(cx - S(42), cy + S(51)), (cx + S(42), cy + S(51))], fill=c_skirt_stripe, width=S(1.5))

        # Shirt torso
        draw.ellipse([cx - S(38), cy - S(12), cx + S(38), cy + S(34)], fill=c_cloth, outline=c_cloth_shade, width=S(1.5))

        # Sailor collar
        collar = [
            (cx - S(26), cy - S(6)),
            (cx + S(26), cy - S(6)),
            (cx + S(18), cy + S(22)),
            (cx, cy + S(27)),
            (cx - S(18), cy + S(22)),
        ]
        draw.polygon(collar, fill=c_collar_white, outline=(215, 218, 226, 255), width=S(1.2))

        # Red ribbon tie
        draw.polygon([(cx - S(16), cy + S(12)), (cx - S(2), cy + S(16)), (cx - S(14), cy + S(26))], fill=c_red_ribbon)
        draw.polygon([(cx + S(16), cy + S(12)), (cx + S(2), cy + S(16)), (cx + S(14), cy + S(26))], fill=c_red_ribbon)
        draw.ellipse([cx - S(4.5), cy + S(13), cx + S(4.5), cy + S(20)], fill=c_ribbon_knot)

        # 4. Golden Head & Face
        draw.ellipse([cx - S(40), head_y - S(32), cx + S(40), head_y + S(32)], fill=c_gold, outline=c_gold_outline, width=S(1.8))

        # Eyes
        if eye_state == "closed":
            draw.arc([cx - S(25), head_y - S(4), cx - S(11), head_y + S(8)], 15, 165, fill=c_eye, width=S(2.8))
            draw.arc([cx + S(11), head_y - S(4), cx + S(25), head_y + S(8)], 15, 165, fill=c_eye, width=S(2.8))
        elif eye_state == "sad":
            draw.line([(cx - S(23), head_y - S(4)), (cx - S(13), head_y + S(6))], fill=c_eye, width=S(2.5))
            draw.line([(cx - S(23), head_y + S(6)), (cx - S(13), head_y - S(4))], fill=c_eye, width=S(2.5))
            draw.line([(cx + S(13), head_y - S(4)), (cx + S(23), head_y + S(6))], fill=c_eye, width=S(2.5))
            draw.line([(cx + S(13), head_y + S(6)), (cx + S(23), head_y - S(4))], fill=c_eye, width=S(2.5))
        else:
            draw.ellipse([cx - S(25), head_y - S(9), cx - S(11), head_y + S(8)], fill=c_eye)
            draw.ellipse([cx + S(11), head_y - S(9), cx + S(25), head_y + S(8)], fill=c_eye)
            # Highlights
            draw.ellipse([cx - S(23), head_y - S(7), cx - S(17), head_y - S(1)], fill=(255, 255, 255, 255))
            draw.ellipse([cx + S(13), head_y - S(7), cx + S(19), head_y - S(1)], fill=(255, 255, 255, 255))
            draw.ellipse([cx - S(16), head_y + S(2), cx - S(13), head_y + S(5)], fill=(255, 255, 255, 220))
            draw.ellipse([cx + S(20), head_y + S(2), cx + S(23), head_y + S(5)], fill=(255, 255, 255, 220))

        # Nose & 'w' mouth
        draw.polygon([(cx - S(6.5), head_y + S(9)), (cx + S(6.5), head_y + S(9)), (cx, head_y + S(15.5))], fill=c_eye)
        draw.arc([cx - S(9), head_y + S(12), cx, head_y + S(21)], 0, 180, fill=c_eye, width=S(1.8))
        draw.arc([cx, head_y + S(12), cx + S(9), head_y + S(21)], 0, 180, fill=c_eye, width=S(1.8))

        # Solid light pink blush (NON-TRANSPARENT, clean & lovely!)
        c_blush = (255, 185, 192, 255)
        draw.ellipse([cx - S(32), head_y + S(9), cx - S(20), head_y + S(19)], fill=c_blush)
        draw.ellipse([cx + S(20), head_y + S(9), cx + S(32), head_y + S(19)], fill=c_blush)

        # 5. Bangs under cap (matching perfect_hat.png, no hairpin)
        draw.ellipse([cx - S(30), head_y - S(20), cx - S(10), head_y - S(3)], fill=c_hair, outline=c_hair_shade, width=S(1))
        draw.ellipse([cx - S(14), head_y - S(20), cx + S(12), head_y - S(2)], fill=c_hair, outline=c_hair_shade, width=S(1))
        draw.ellipse([cx + S(8), head_y - S(20), cx + S(28), head_y - S(4)], fill=c_hair, outline=c_hair_shade, width=S(1))
        draw.polygon([(cx - S(38), head_y - S(15)), (cx - S(45), head_y + S(22)), (cx - S(34), head_y + S(15))], fill=c_hair)
        draw.polygon([(cx + S(38), head_y - S(15)), (cx + S(45), head_y + S(22)), (cx + S(34), head_y + S(15))], fill=c_hair)

        # 6. BASEBALL CAP (Exact geometry and structure from perfect_hat.png)
        cap_poly = [
            (cx - S(42), head_y - S(14)),
            (cx - S(44), head_y - S(36)),
            (cx - S(28), head_y - S(56)),
            (cx + S(18), head_y - S(56)),
            (cx + S(40), head_y - S(36)),
            (cx + S(38), head_y - S(14)),
        ]
        draw.polygon(cap_poly, fill=c_cap, outline=c_cap_shade, width=S(2))
        draw.chord([cx - S(42), head_y - S(58), cx + S(38), head_y - S(14)], 180, 360, fill=c_cap)
        # Top button
        draw.ellipse([cx - S(4), head_y - S(60), cx + S(4), head_y - S(53)], fill=c_cap, outline=c_cap_light, width=S(1))
        # Cap seam highlights
        draw.arc([cx - S(34), head_y - S(56), cx + S(28), head_y - S(18)], 200, 320, fill=c_cap_light, width=S(1.2))

        # Visor / Bill (Curving forward from cap front)
        visor_poly = [
            (cx - S(52), head_y - S(16)),
            (cx + S(16), head_y - S(20)),
            (cx + S(18), head_y - S(10)),
            (cx - S(10), head_y - S(5)),
            (cx - S(48), head_y - S(6)),
        ]
        draw.polygon(visor_poly, fill=c_visor, outline=c_cap_shade, width=S(2))

        # White infinity logo 'oo'
        draw.ellipse([cx + S(7), head_y - S(40), cx + S(17), head_y - S(30)], outline=(245, 245, 250, 255), width=S(2))
        draw.ellipse([cx + S(15), head_y - S(40), cx + S(25), head_y - S(30)], outline=(245, 245, 250, 255), width=S(2))

        # 7. ELECTRIC GUITAR & DOGGY PAWS
        draw.line([(cx + S(18), cy + S(4)), (cx - S(16), cy + S(44))], fill=(42, 42, 46, 255), width=S(7))

        # Neck
        nx1, ny1 = cx - S(16), cy + S(28)
        nx2, ny2 = cx + S(68), cy - S(4)
        draw.line([(nx1, ny1), (nx2, ny2)], fill=c_neck, width=S(10))
        draw.line([(nx1, ny1), (nx2, ny2)], fill=c_fret, width=S(1.2))
        for f_i in range(5):
            fx = nx1 + (nx2 - nx1) * (f_i + 1) / 6.0
            fy = ny1 + (ny2 - ny1) * (f_i + 1) / 6.0
            draw.line([(fx - S(2), fy - S(4)), (fx + S(2), fy + S(4))], fill=c_fret, width=S(1))

        # Headstock
        headstock_poly = [
            (cx + S(64), cy - S(8)),
            (cx + S(78), cy - S(14)),
            (cx + S(74), cy + S(2)),
            (cx + S(60), cy + S(3)),
        ]
        draw.polygon(headstock_poly, fill=c_guitar, outline=c_cap_shade, width=S(1.5))
        for p_i in range(4):
            px = cx + S(62 + p_i * 4)
            py = cy - S(12 + p_i * 2)
            draw.ellipse([px, py, px + S(3.5), py + S(3.5)], fill=c_pickguard)

        # Guitar Body
        gx, gy = cx - S(24), cy + S(34)
        draw.ellipse([gx - S(25), gy - S(17), gx + S(25), gy + S(23)], fill=c_guitar, outline=c_cap_shade, width=S(2))
        draw.polygon([(gx - S(20), gy - S(12)), (gx - S(13), gy - S(25)), (gx, gy - S(14))], fill=c_guitar)
        draw.polygon([(gx + S(10), gy - S(10)), (gx + S(19), gy - S(19)), (gx + S(21), gy - S(4))], fill=c_guitar)

        # Pickguard & Knobs
        draw.ellipse([gx - S(8), gy - S(6), gx + S(11), gy + S(11)], fill=c_pickguard)
        draw.rectangle([gx - S(13), gy - S(2), gx + S(5), gy + S(3)], fill=(255, 225, 100, 255))
        draw.ellipse([gx - S(16), gy + S(6), gx - S(11), gy + S(11)], fill=c_knob)
        draw.ellipse([gx - S(8), gy + S(11), gx - S(3), gy + S(16)], fill=c_knob)

        # Paws
        # Left paw: GENTLY SLIDES ALONG NECK in working mode!
        fret_dx = S(fret_slide)
        fret_dy = -int(fret_dx * 0.38)
        lp_x = cx + S(24) + fret_dx
        lp_y = cy + S(8) + fret_dy
        draw.ellipse([lp_x, lp_y, lp_x + S(13), lp_y + S(13)], fill=c_gold, outline=c_gold_outline, width=S(1.8))
        draw.arc([lp_x + S(4), lp_y + S(6), lp_x + S(14), lp_y + S(16)], 160, 340, fill=c_collar_white, width=S(2.2))

        # Right paw: RAISED SLIGHTLY (midpoint between initial and lowered position)
        strum_x = cx - S(25)
        strum_y = cy + S(22) + strum_offset
        draw.ellipse([strum_x, strum_y, strum_x + S(13), strum_y + S(13)], fill=c_gold, outline=c_gold_outline, width=S(1.8))
        # Red guitar pick
        draw.polygon([(strum_x + S(9), strum_y + S(6)), (strum_x + S(15), strum_y + S(11)), (strum_x + S(11), strum_y + S(14))], fill=(255, 80, 80, 255))
        draw.arc([strum_x - S(1), strum_y + S(6), strum_x + S(9), strum_y + S(16)], 160, 340, fill=c_collar_white, width=S(2.2))

        if extra_decor:
            extra_decor(draw, cx, cy, head_y, S)

        # Downscale with LANCZOS for silky vectors
        downscaled = canvas.resize((W, H), Image.Resampling.LANCZOS)
        if rot != 0.0:
            downscaled = downscaled.rotate(rot, resample=Image.Resampling.BICUBIC, center=(W // 2, H - 8))

        # HARD ALPHA THRESHOLD: Completely banishes dirty semi-transparent black borders on Windows!
        r, g, b, a = downscaled.split()
        a = a.point(lambda p: 255 if p > 90 else 0)
        return Image.merge("RGBA", (r, g, b, a))

    def draw_star(draw: ImageDraw.ImageDraw, sx: int, sy: int, size: int, color):
        pts = [
            (sx, sy - size), (sx + int(size * 0.3), sy - int(size * 0.3)),
            (sx + size, sy), (sx + int(size * 0.3), sy + int(size * 0.3)),
            (sx, sy + size), (sx - int(size * 0.3), sy + int(size * 0.3)),
            (sx - size, sy), (sx - int(size * 0.3), sy - int(size * 0.3)),
        ]
        draw.polygon(pts, fill=color)

    def draw_note(draw: ImageDraw.ImageDraw, nx: int, ny: int, size: int, color, S):
        draw.ellipse([nx, ny, nx + size, ny + int(size * 0.75)], fill=color)
        draw.line([(nx + size, ny + int(size * 0.35)), (nx + size, ny - int(size * 1.2))], fill=color, width=S(1.8))
        draw.arc([nx + size, ny - int(size * 1.4), nx + int(size * 1.7), ny - int(size * 0.4)], 250, 360, fill=color, width=S(1.8))

    def draw_double_note(draw: ImageDraw.ImageDraw, nx: int, ny: int, size: int, color, S):
        draw.ellipse([nx, ny, nx + size, ny + int(size * 0.75)], fill=color)
        draw.ellipse([nx + int(size * 1.4), ny - int(size * 0.3), nx + int(size * 2.4), ny + int(size * 0.45)], fill=color)
        draw.line([(nx + size, ny + int(size * 0.35)), (nx + size, ny - int(size * 1.2))], fill=color, width=S(1.8))
        draw.line([(nx + int(size * 2.4), ny + int(size * 0.05)), (nx + int(size * 2.4), ny - int(size * 1.5))], fill=color, width=S(1.8))
        draw.line([(nx + size, ny - int(size * 1.2)), (nx + int(size * 2.4), ny - int(size * 1.5))], fill=color, width=S(2.2))

    def draw_sweat(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int):
        color = (60, 160, 245, 230)
        draw.ellipse([cx - size // 2, cy, cx + size // 2, cy + size], fill=color)
        draw.polygon([(cx - size // 2 + 1, cy + size // 2), (cx + size // 2 - 1, cy + size // 2), (cx, cy - size // 2)], fill=color)

    states = ["idle", "running", "waiting", "failed", "review", "waving"]
    frames: Dict[str, List[Image.Image]] = {}

    for state in states:
        state_frames = []
        count = 8 if state == "failed" else 6
        for i in range(count):
            if state == "idle":
                # Idle: Gentle breathing bob + subtle guitar sway, blink on frame 3
                bob = int(2.5 * math.sin(i * 2 * math.pi / 6))
                rot = 1.0 * math.sin(i * 2 * math.pi / 6)
                eye = "closed" if i == 3 else "open"

                f = draw_character_frame(bob_y=bob, rot=rot, eye_state=eye, strum_offset=0, fret_slide=0.0)

            elif state == "running":
                # WORKING STATE: Passionate guitar jamming!
                # 1. Double-tempo body bounce & rocking tilt
                bob = int(4.5 * math.sin(i * 2 * math.pi / 3))
                rot = 3.5 * math.sin(i * 2 * math.pi / 6)
                # 2. Lowered right strumming paw picking fast
                strum_p = [0, -S(4), S(5), -S(3), S(4), -S(1)]
                s_off = strum_p[i % len(strum_p)]
                # 3. Left paw slides slowly along guitar neck (smooth sine shifting)
                f_slide = 3.5 * math.sin(i * 2 * math.pi / 6)

                def add_working_decor(draw, cx, cy, head_y, S):
                    # Animated flying note high above headstock
                    n1_y = cy - S(32) - S(int(5 * math.sin(i * math.pi / 3)))
                    n1_x = cx + S(66) + S(int(2 * math.cos(i)))
                    c1 = (255, 195, 45, 240) if (i % 2 == 0) else (255, 140, 50, 240)
                    draw_note(draw, n1_x, n1_y, S(6), c1, S)

                    # Dynamic double note floating high above left side
                    n2_y = head_y - S(40) - S(int(6 * math.sin((i + 2) * math.pi / 3)))
                    n2_x = cx - S(46)
                    c2 = (255, 120, 140, 240) if (i % 2 == 1) else (255, 215, 60, 240)
                    draw_double_note(draw, n2_x, n2_y, S(5), c2, S)

                    # Golden flash sparkle
                    star_s = S(5 if i % 2 == 0 else 7)
                    draw_star(draw, cx + S(56), cy - S(10), star_s, (255, 225, 60, 245))
                    draw_star(draw, cx - S(36), head_y - S(46), S(4), (255, 210, 50, 220))

                f = draw_character_frame(bob_y=bob, rot=rot, eye_state="open", strum_offset=s_off, fret_slide=f_slide, extra_decor=add_working_decor)

            elif state == "waiting":
                # Thinking: Calm breathing + floating thought dots
                bob = int(1.5 * math.sin(i * 2 * math.pi / 6))

                def add_thinking_dots(draw, cx, cy, head_y, S):
                    num_dots = (i % 3) + 1
                    for d in range(num_dots):
                        draw.ellipse([cx + S(28) + d * S(7), head_y - S(38), cx + S(33) + d * S(7), head_y - S(33)], fill=(120, 160, 240, 220))

                f = draw_character_frame(bob_y=bob, rot=1.5, eye_state="open", strum_offset=0, fret_slide=0.0, extra_decor=add_thinking_dots)

            elif state == "failed":
                # Fixing / Error: Jitter + sweat drop
                jitter = int(2.5 * ((i % 2) * 2 - 1))

                def add_sweat_drop(draw, cx, cy, head_y, S):
                    draw_sweat(draw, cx + S(34), head_y - S(10) + S(int((i % 3) * 2)), S(8))

                f = draw_character_frame(bob_y=0, rot=0, eye_state="sad", strum_offset=0, fret_slide=0.0, extra_decor=add_sweat_drop)
                if jitter != 0:
                    shifted = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                    shifted.paste(f, (jitter, 0), f)
                    f = shifted

            elif state == "review":
                # Success: Big joyful jump + golden stars
                jump = -abs(int(12 * math.sin(i * math.pi / 6)))
                rot = 1.5 * math.sin(i * 2 * math.pi / 6)

                def add_celebration_stars(draw, cx, cy, head_y, S):
                    draw_star(draw, cx - S(42), head_y - S(25), S(7), (255, 215, 0, 240))
                    draw_star(draw, cx + S(42), head_y - S(30), S(8), (255, 215, 0, 240))

                f = draw_character_frame(bob_y=jump, rot=rot, eye_state="open", strum_offset=0, fret_slide=0.0, extra_decor=add_celebration_stars)

            elif state == "waving":
                # Greeting: Lively greeting sway
                rot = 3.5 * math.sin(i * 2 * math.pi / 6)
                f = draw_character_frame(bob_y=0, rot=rot, eye_state="open", strum_offset=0, fret_slide=0.0)

            state_frames.append(f)
        frames[state] = state_frames

    return PetSkin("default-doloris", "多洛莉丝 (Default)", frames)


def create_procedural_golden_skin() -> PetSkin:
    """Generates the legacy procedural Golden Retriever pet skin as fallback."""
    states = ["idle", "running", "waiting", "failed", "review", "waving"]
    frames: Dict[str, List[Image.Image]] = {}

    w, h = CODEX_V2_CELL_W, CODEX_V2_CELL_H

    for state in states:
        state_frames = []
        count = 6 if state in ("idle", "running", "waiting", "review") else (8 if state == "failed" else 4)

        for i in range(count):
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            cx, cy = w // 2, h // 2 + 10
            bob = int(4 * ((i % 2) * 2 - 1)) if state in ("idle", "running") else 0

            body_color = (245, 185, 75, 255)
            dark_gold = (210, 150, 45, 255)
            ear_color = (195, 130, 35, 255)

            draw.ellipse([cx - 45, cy - 10 + bob, cx + 45, cy + 55 + bob], fill=body_color, outline=dark_gold, width=3)
            head_y = cy - 45 + bob
            draw.ellipse([cx - 40, head_y - 35, cx + 40, head_y + 35], fill=body_color, outline=dark_gold, width=3)
            draw.ellipse([cx - 52, head_y - 30, cx - 30, head_y + 25], fill=ear_color, outline=dark_gold, width=2)
            draw.ellipse([cx + 30, head_y - 30, cx + 52, head_y + 25], fill=ear_color, outline=dark_gold, width=2)

            eye_color = (35, 25, 20, 255)
            if state == "failed":
                draw.line([cx - 24, head_y - 6, cx - 12, head_y + 6], fill=eye_color, width=3)
                draw.line([cx - 24, head_y + 6, cx - 12, head_y - 6], fill=eye_color, width=3)
                draw.line([cx + 12, head_y - 6, cx + 24, head_y + 6], fill=eye_color, width=3)
                draw.line([cx + 12, head_y + 6, cx + 24, head_y - 6], fill=eye_color, width=3)
            elif state == "waiting":
                draw.ellipse([cx - 25, head_y - 12, cx - 13, head_y + 4], fill=eye_color)
                draw.ellipse([cx + 13, head_y - 12, cx + 25, head_y + 4], fill=eye_color)
                draw.ellipse([cx - 20, head_y - 10, cx - 15, head_y - 5], fill=(255, 255, 255, 255))
                draw.ellipse([cx + 18, head_y - 10, cx + 23, head_y - 5], fill=(255, 255, 255, 255))
            else:
                if state == "idle" and i == 3:
                    draw.line([cx - 25, head_y - 2, cx - 11, head_y - 2], fill=eye_color, width=3)
                    draw.line([cx + 11, head_y - 2, cx + 25, head_y - 2], fill=eye_color, width=3)
                else:
                    draw.ellipse([cx - 24, head_y - 10, cx - 12, head_y + 2], fill=eye_color)
                    draw.ellipse([cx + 12, head_y - 10, cx + 24, head_y + 2], fill=eye_color)
                    draw.ellipse([cx - 19, head_y - 8, cx - 15, head_y - 4], fill=(255, 255, 255, 255))
                    draw.ellipse([cx + 17, head_y - 8, cx + 21, head_y - 4], fill=(255, 255, 255, 255))

            draw.polygon([(cx - 7, head_y + 10), (cx + 7, head_y + 10), (cx, head_y + 17)], fill=(40, 30, 25, 255))
            draw.arc([cx - 10, head_y + 12, cx, head_y + 24], start=0, end=180, fill=eye_color, width=2)
            draw.arc([cx, head_y + 12, cx + 10, head_y + 24], start=0, end=180, fill=eye_color, width=2)

            if state == "running":
                hat_y = head_y - 45
                draw.chord([cx - 36, hat_y - 20, cx + 36, hat_y + 25], start=180, end=360, fill=(250, 205, 30, 255), outline=(190, 150, 10, 255), width=2)
                draw.rectangle([cx - 42, hat_y + 2, cx + 42, hat_y + 8], fill=(250, 205, 30, 255), outline=(190, 150, 10, 255), width=2)
            elif state == "review":
                draw.polygon([(cx - 10, head_y + 35), (cx + 10, head_y + 35), (cx, head_y + 48)], fill=(230, 50, 50, 255))
                draw.ellipse([cx - 8, head_y + 26, cx + 8, head_y + 42], fill=(255, 215, 0, 255), outline=(200, 160, 0, 255), width=2)

            state_frames.append(img)
        frames[state] = state_frames

    return PetSkin("default-golden", "金毛小代班 (旧版图形)", frames)


def create_default_pet_skin() -> PetSkin:
    """Returns the primary procedural Doloris default pet skin."""
    if not HAS_PIL:
        raise RuntimeError("Pillow is required for pet graphics. Install it with: pip install Pillow")
    try:
        return create_procedural_doloris_skin()
    except Exception:
        return create_procedural_golden_skin()


def discover_available_pets() -> List[PetSkin]:
    """Scans ~/.codex/pets/ and local folders to discover available pet skins."""
    if not HAS_PIL:
        return []
    default_skin = create_default_pet_skin()
    skins: List[PetSkin] = [default_skin]
    known_names = {default_skin.name, default_skin.display_name}

    # Also make legacy procedural golden available in switch menu
    legacy_golden = create_procedural_golden_skin()
    if legacy_golden.name not in known_names:
        skins.append(legacy_golden)
        known_names.add(legacy_golden.name)
        known_names.add(legacy_golden.display_name)

    # 1. Check ~/.codex/pets/
    codex_home = os.environ.get("CODEX_HOME")
    pets_dir = Path(codex_home) / "pets" if codex_home else Path.home() / ".codex" / "pets"

    if pets_dir.is_dir():
        for p in pets_dir.iterdir():
            if p.is_dir():
                skin = load_folder_pet(p)
                if skin and skin.name not in known_names and skin.display_name not in known_names:
                    skins.append(skin)
                    known_names.add(skin.name)
                    known_names.add(skin.display_name)

    # 2. Check local ./pets/ in current workspace
    local_pets = Path.cwd() / "pets"
    if local_pets.is_dir():
        for p in local_pets.iterdir():
            if p.is_dir():
                skin = load_folder_pet(p)
                if skin and skin.name not in known_names and skin.display_name not in known_names:
                    skins.append(skin)
                    known_names.add(skin.name)
                    known_names.add(skin.display_name)

    return skins
