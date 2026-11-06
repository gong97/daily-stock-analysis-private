# -*- coding: utf-8 -*-
"""model_scope 换档时的降级链。"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.tiered_analysis import model_scope


class TestModelScope(unittest.TestCase):

    def test_duplicate_fallback_replaced_by_original_primary(self):
        lite, flash = "gemini/gemini-3.5-flash-lite", "gemini/gemini-3.5-flash"
        config = SimpleNamespace(litellm_model=flash, litellm_fallback_models=[lite])
        with model_scope(config, lite):
            self.assertEqual(config.litellm_model, lite)
            self.assertEqual(config.litellm_fallback_models, [flash])
        self.assertEqual(config.litellm_model, flash)
        self.assertEqual(config.litellm_fallback_models, [lite])

    def test_same_model_keeps_chain(self):
        config = SimpleNamespace(litellm_model="a", litellm_fallback_models=["b", "c"])
        with model_scope(config, "a"):
            self.assertEqual(config.litellm_fallback_models, ["b", "c"])

    def test_other_fallbacks_kept_after_original(self):
        config = SimpleNamespace(litellm_model="a", litellm_fallback_models=["b", "c"])
        with model_scope(config, "c"):
            self.assertEqual(config.litellm_fallback_models, ["a", "b"])
        self.assertEqual(config.litellm_fallback_models, ["b", "c"])

    def test_empty_model_is_noop(self):
        config = SimpleNamespace(litellm_model="a", litellm_fallback_models=["b"])
        with model_scope(config, ""):
            self.assertEqual(config.litellm_fallback_models, ["b"])


if __name__ == "__main__":
    unittest.main()
