"""Tests for Doloris Desktop Pet Loader and Codex V2 Sprite Slicer."""

import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore
    HAS_PIL = False

from doloris_app.pet_loader import (
    CODEX_V2_CELL_H,
    CODEX_V2_CELL_W,
    CODEX_V2_COLS,
    CODEX_V2_ROWS,
    create_default_pet_skin,
    discover_available_pets,
    load_folder_pet,
    slice_codex_v2_atlas,
)


@unittest.skipUnless(HAS_PIL, "Pillow is required for pet loader tests")
class TestPetLoader(unittest.TestCase):
    def test_default_pet_skin_generation(self):
        skin = create_default_pet_skin()
        self.assertEqual(skin.name, "default-doloris")
        self.assertIn("idle", skin.frames)
        self.assertIn("running", skin.frames)
        self.assertIn("waiting", skin.frames)
        self.assertIn("failed", skin.frames)
        self.assertIn("review", skin.frames)

        idle_frames = skin.get_frames("idle")
        self.assertGreaterEqual(len(idle_frames), 4)
        for frame in idle_frames:
            self.assertEqual(frame.size, (CODEX_V2_CELL_W, CODEX_V2_CELL_H))
            self.assertEqual(frame.mode, "RGBA")

    def test_slice_codex_v2_atlas(self):
        # Create a mock 1536x2288 atlas
        atlas_w = CODEX_V2_COLS * CODEX_V2_CELL_W
        atlas_h = CODEX_V2_ROWS * CODEX_V2_CELL_H

        mock_atlas = Image.new("RGBA", (atlas_w, atlas_h), (100, 150, 200, 255))

        with tempfile.TemporaryDirectory() as tmp_dir:
            atlas_path = Path(tmp_dir) / "spritesheet.png"
            mock_atlas.save(atlas_path)

            sliced = slice_codex_v2_atlas(atlas_path)

            self.assertEqual(len(sliced["idle"]), 6)
            self.assertEqual(len(sliced["running-right"]), 8)
            self.assertEqual(len(sliced["running-left"]), 8)
            self.assertEqual(len(sliced["waving"]), 4)
            self.assertEqual(len(sliced["jumping"]), 5)
            self.assertEqual(len(sliced["failed"]), 8)
            self.assertEqual(len(sliced["waiting"]), 6)
            self.assertEqual(len(sliced["running"]), 6)
            self.assertEqual(len(sliced["review"]), 6)

            for state, frames in sliced.items():
                for f in frames:
                    self.assertEqual(f.size, (CODEX_V2_CELL_W, CODEX_V2_CELL_H))

    def test_load_folder_pet_with_spritesheet(self):
        atlas_w = CODEX_V2_COLS * CODEX_V2_CELL_W
        atlas_h = CODEX_V2_ROWS * CODEX_V2_CELL_H
        mock_atlas = Image.new("RGBA", (atlas_w, atlas_h), (255, 0, 0, 255))

        with tempfile.TemporaryDirectory() as tmp_dir:
            folder = Path(tmp_dir) / "my_pet"
            folder.mkdir()
            mock_atlas.save(folder / "spritesheet.png")

            # Write pet.json
            with open(folder / "pet.json", "w", encoding="utf-8") as f:
                f.write('{"id": "my_pet", "displayName": "My Test Pet"}')

            skin = load_folder_pet(folder)
            self.assertIsNotNone(skin)
            self.assertEqual(skin.display_name, "My Test Pet")
            self.assertEqual(len(skin.get_frames("working")), 6)

    def test_discover_available_pets_always_contains_default(self):
        pets = discover_available_pets()
        self.assertGreaterEqual(len(pets), 1)
        self.assertEqual(pets[0].name, "default-doloris")


if __name__ == "__main__":
    unittest.main()
