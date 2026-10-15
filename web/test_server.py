import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class BlockLabTests(unittest.TestCase):
    def test_gamemode_is_discovered_from_datapack_zip(self):
        modes = server.available_modes()
        self.assertIn('place_to_break', [m['id'] for m in modes])
        pack = next(m for m in modes if m['id'] == 'place_to_break')
        self.assertEqual(pack['datapack'], 'place_to_break.zip')

    def test_place_to_break_datapack_has_metadata_and_load_function(self):
        path = server.GAMEMODES / 'place_to_break.zip'
        self.assertTrue(path.is_file())
        with zipfile.ZipFile(path) as archive:
            self.assertIn('pack.mcmeta', archive.namelist())
            self.assertIn('data/custom/function/load.mcfunction', archive.namelist())
            self.assertIn('data/minecraft/tags/function/load.json', archive.namelist())
            self.assertIsNone(archive.testzip())

    def test_world_names_are_sanitized(self):
        self.assertEqual(server.safe_world_name('World Place To Break'), 'world-place-to-break')
        self.assertEqual(server.safe_world_name('test_56'), 'test_56')
        with self.assertRaises(ValueError):
            server.safe_world_name('../')
        with self.assertRaises(ValueError):
            server.safe_world_name('')


if __name__ == '__main__':
    unittest.main()
