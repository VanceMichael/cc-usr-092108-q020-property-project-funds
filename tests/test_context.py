import unittest
from pathlib import Path
from src.context import load_context

class ContextTest(unittest.TestCase):
    def test_fixture_matches_domain(self):
        value = load_context(Path("fixtures/context.json"))
        self.assertEqual(value["domain"], "property-project-funds")
        self.assertGreaterEqual(len(value["constraints"]), 1)

if __name__ == "__main__":
    unittest.main()
