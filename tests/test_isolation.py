import unittest

from tests.support import Base, fresh_server


class IsolationTest(Base):
    """No test may write to the real data folder (/data, or C:\\data on Windows): a forgotten patch of a setting
    must not reach it, also after the server parts are loaded again."""

    def test_the_settings_file_and_the_data_folder_are_the_test_s_own(self):
        from stack_ui import auth, core
        for _ in range(2):              # as imported, and after a fresh load
            self.assertTrue(auth.SETTINGS_FILE.is_relative_to(self.tmp), auth.SETTINGS_FILE)
            self.assertTrue(core.DATA.is_relative_to(self.tmp), core.DATA)
            fresh_server()

    def test_a_token_written_without_patching_lands_in_the_temp_folder(self):
        ui = fresh_server()
        ui.api_token({})
        self.assertTrue(ui.auth.SETTINGS_FILE.is_file())
        self.assertTrue(ui.auth.SETTINGS_FILE.is_relative_to(self.tmp))


if __name__ == '__main__':
    unittest.main()
