import importlib
import unittest

from tests.support import Base, fresh_server

# every module that keeps something in the data folder: each reads it through stacks.DATA, none keeps a copy
DATA_USERS = ('core', 'auth', 'rollback', 'schedule', 'activity', 'backups', 'lifecycle', 'stackops', 'autoupdate')


class IsolationTest(Base):
    """No test may write to the real data folder (/data, or C:\\data on Windows): a forgotten patch of a setting
    must not reach it, also after the server parts are loaded again."""

    def test_the_settings_file_and_the_data_folder_are_the_test_s_own(self):
        from stack_ui import auth
        from stack_ui import stacks as st
        for _ in range(2):              # as imported, and after a fresh load
            self.assertTrue(auth.SETTINGS_FILE.is_relative_to(self.tmp), auth.SETTINGS_FILE)
            self.assertTrue(st.DATA.is_relative_to(self.tmp), st.DATA)
            fresh_server()

    def test_a_token_written_without_patching_lands_in_the_temp_folder(self):
        ui = fresh_server()
        ui.api_token({})
        self.assertTrue(ui.auth.SETTINGS_FILE.is_file())
        self.assertTrue(ui.auth.SETTINGS_FILE.is_relative_to(self.tmp))

    def test_the_data_folder_is_named_in_one_place(self):
        """stacks.DATA only: a module with its own DATA is one more thing a test has to point away from /data."""
        fresh_server()
        for name in DATA_USERS:
            module = importlib.import_module(f'stack_ui.{name}')
            self.assertFalse(hasattr(module, 'DATA'), f'stack_ui.{name} keeps its own DATA')

    def test_one_patch_moves_every_file_of_the_data_folder(self):
        from datetime import datetime
        from stack_ui import activity, backups, schedule
        from stack_ui import stacks as st
        ui = fresh_server()
        elsewhere = self.tmp / 'elsewhere'
        self.patch(st, 'DATA', elsewhere)
        activity.record({'type': 'change', 'kind': 'image-pull', 'target': 'x', 'result': 'ok'})
        schedule.set_auto('web', True)
        self.assertTrue((elsewhere / 'activity.jsonl').is_file())
        self.assertTrue((elsewhere / 'schedule.json').is_file())
        _, archive = backups.build(datetime.now())
        self.assertGreater(len(archive), 0)
        ui.api_token({})                             # the settings file has its own setting (STACK_UI_SETTINGS_FILE)
        self.assertTrue(ui.auth.SETTINGS_FILE.is_relative_to(self.tmp))


if __name__ == '__main__':
    unittest.main()
