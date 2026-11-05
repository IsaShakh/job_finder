import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_finder.config import Config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def load(self, **env):
        with patch.dict(os.environ, env, clear=True):
            return Config.load(self.root)

    def test_dry_defaults_do_not_create_state_or_require_secrets(self):
        config = self.load()
        self.assertEqual(config.owner_id, 0)
        self.assertEqual(config.sources, ('hh',))
        self.assertEqual(config.max_search_pages, 100)
        self.assertEqual(config.db_path, self.root / '.state' / 'agent.sqlite3')
        self.assertEqual(config.api_token_path, self.root / '.state' / 'api-token.json')
        self.assertFalse(config.state_dir.exists())

    def test_dotenv_is_root_scoped_and_environment_wins(self):
        (self.root / '.env').write_text('TELEGRAM_OWNER_ID=123\nSOURCES=hh,fake\nMAX_SEARCH_PAGES=20\n')
        config = self.load(TELEGRAM_OWNER_ID='456')
        self.assertEqual(config.owner_id, 456)
        self.assertEqual(config.sources, ('hh', 'fake'))
        self.assertEqual(config.max_search_pages, 20)

    def test_invalid_values_are_rejected_without_echoing_secrets(self):
        for env in ({'TELEGRAM_OWNER_ID': '-1'}, {'TELEGRAM_OWNER_ID': 'abc'},
                    {'MAX_SEARCH_PAGES': '-1'},
                    {'LLM_BASE_URL': 'http://example.com/v1'},
                    {'LLM_BASE_URL': 'https://secret@example.com/v1'},
                    {'LLM_BASE_URL': 'https://example.com/v1?key=secret'}):
            with self.subTest(env=env), self.assertRaises(ValueError) as raised:
                self.load(**env)
            self.assertNotIn('secret', str(raised.exception))

    def test_repr_never_contains_credentials(self):
        config = self.load(TELEGRAM_BOT_TOKEN='123:private-token', LLM_API_KEY='private-key')
        self.assertNotIn('private-token', repr(config))
        self.assertNotIn('private-key', repr(config))

    def test_model_is_unset_until_user_configures_it(self):
        self.assertEqual(self.load().llm_model, '')
        config = self.load(LLM_BASE_URL='https://models.example/v1/', LLM_MODEL='chosen-model')
        self.assertEqual(config.llm_base_url, 'https://models.example/v1')
        self.assertEqual(config.llm_model, 'chosen-model')
