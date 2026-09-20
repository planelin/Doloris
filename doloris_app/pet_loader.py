"""Codex V2 Pet Atlas parser and custom sprite pack loader for Doloris."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image, ImageDraw

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


def create_default_pet_skin() -> PetSkin:
    """Generates a procedural cute Golden Retriever / Cyber Pup pet skin."""
    states = ["idle", "running", "waiting", "failed", "review", "waving"]
    frames: Dict[str, List[Image.Image]] = {}

    w, h = CODEX_V2_CELL_W, CODEX_V2_CELL_H

    for state in states:
        state_frames = []
        count = 6 if state in ("idle", "running", "waiting", "review") else (8 if state == "failed" else 4)

        for i in range(count):
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Center coordinates
            cx, cy = w // 2, h // 2 + 10

            # Subtle bobbing animation
            bob = int(4 * ((i % 2) * 2 - 1)) if state in ("idle", "running") else 0

            # Body color: Golden Retriever warm gold
            body_color = (245, 185, 75, 255)
            dark_gold = (210, 150, 45, 255)
            ear_color = (195, 130, 35, 255)

            # Draw body
            draw.ellipse([cx - 45, cy - 10 + bob, cx + 45, cy + 55 + bob], fill=body_color, outline=dark_gold, width=3)

            # Draw head
            head_y = cy - 45 + bob
            draw.ellipse([cx - 40, head_y - 35, cx + 40, head_y + 35], fill=body_color, outline=dark_gold, width=3)

            # Draw ears
            draw.ellipse([cx - 52, head_y - 30, cx - 30, head_y + 25], fill=ear_color, outline=dark_gold, width=2)
            draw.ellipse([cx + 30, head_y - 30, cx + 52, head_y + 25], fill=ear_color, outline=dark_gold, width=2)

            # Draw eyes
            eye_color = (35, 25, 20, 255)
            if state == "failed":
                # Sad closed eyes (X X)
                draw.line([cx - 24, head_y - 6, cx - 12, head_y + 6], fill=eye_color, width=3)
                draw.line([cx - 24, head_y + 6, cx - 12, head_y - 6], fill=eye_color, width=3)
                draw.line([cx + 12, head_y - 6, cx + 24, head_y + 6], fill=eye_color, width=3)
                draw.line([cx + 12, head_y + 6, cx + 24, head_y - 6], fill=eye_color, width=3)
            elif state == "waiting":
                # Wide curious eyes
                draw.ellipse([cx - 25, head_y - 12, cx - 13, head_y + 4], fill=eye_color)
                draw.ellipse([cx + 13, head_y - 12, cx + 25, head_y + 4], fill=eye_color)
                draw.ellipse([cx - 20, head_y - 10, cx - 15, head_y - 5], fill=(255, 255, 255, 255))
                draw.ellipse([cx + 18, head_y - 10, cx + 23, head_y - 5], fill=(255, 255, 255, 255))
            else:
                # Friendly blink or open eyes
                if state == "idle" and i == 3:
                    # Blink
                    draw.line([cx - 25, head_y - 2, cx - 11, head_y - 2], fill=eye_color, width=3)
                    draw.line([cx + 11, head_y - 2, cx + 25, head_y - 2], fill=eye_color, width=3)
                else:
                    draw.ellipse([cx - 24, head_y - 10, cx - 12, head_y + 2], fill=eye_color)
                    draw.ellipse([cx + 12, head_y - 10, cx + 24, head_y + 2], fill=eye_color)
                    draw.ellipse([cx - 19, head_y - 8, cx - 15, head_y - 4], fill=(255, 255, 255, 255))
                    draw.ellipse([cx + 17, head_y - 8, cx + 21, head_y - 4], fill=(255, 255, 255, 255))

            # Draw nose & mouth
            draw.polygon([(cx - 7, head_y + 10), (cx + 7, head_y + 10), (cx, head_y + 17)], fill=(40, 30, 25, 255))
            draw.arc([cx - 10, head_y + 12, cx, head_y + 24], start=0, end=180, fill=eye_color, width=2)
            draw.arc([cx, head_y + 12, cx + 10, head_y + 24], start=0, end=180, fill=eye_color, width=2)

            # Prop / Costume depending on state
            if state == "running":
                # Safety Hardhat for working state!
                hat_y = head_y - 45
                draw.chord([cx - 36, hat_y - 20, cx + 36, hat_y + 25], start=180, end=360, fill=(250, 205, 30, 255), outline=(190, 150, 10, 255), width=2)
                draw.rectangle([cx - 42, hat_y + 2, cx + 42, hat_y + 8], fill=(250, 205, 30, 255), outline=(190, 150, 10, 255), width=2)
            elif state == "review":
                # Tiny medal / ribbon for success!
                draw.polygon([(cx - 10, head_y + 35), (cx + 10, head_y + 35), (cx, head_y + 48)], fill=(230, 50, 50, 255))
                draw.ellipse([cx - 8, head_y + 26, cx + 8, head_y + 42], fill=(255, 215, 0, 255), outline=(200, 160, 0, 255), width=2)

            state_frames.append(img)
        frames[state] = state_frames

    return PetSkin("default-golden", "金毛小代班 (Default)", frames)


def discover_available_pets() -> List[PetSkin]:
    """Scans ~/.codex/pets/ and local folders to discover available pet skins."""
    skins: List[PetSkin] = [create_default_pet_skin()]

    # 1. Check ~/.codex/pets/
    codex_home = os.environ.get("CODEX_HOME")
    pets_dir = Path(codex_home) / "pets" if codex_home else Path.home() / ".codex" / "pets"

    if pets_dir.is_dir():
        for p in pets_dir.iterdir():
            if p.is_dir():
                skin = load_folder_pet(p)
                if skin:
                    skins.append(skin)

    # 2. Check local ./pets/ in current workspace
    local_pets = Path.cwd() / "pets"
    if local_pets.is_dir():
        for p in local_pets.iterdir():
            if p.is_dir():
                skin = load_folder_pet(p)
                if skin:
                    skins.append(skin)

    return skins
