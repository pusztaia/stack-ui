import importlib
import json

from tests.support import Base, compose_yaml, make_stack
from stack_ui import server as srv
from stack_ui import stacks as st


class StackFilesReviewTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)

    def single_yaml_stack(self):
        """A folder found by the 'only *.yaml' rule (no stack.json, no known compose name)."""
        f = make_stack(self.stacks, 'graf', compose_yaml('graf'), compose_name='10-graf.yaml', env_name='graf.env')
        return f, {'composeText': compose_yaml('graf'), 'envText': 'TZ=Europe/Budapest\n'}

    def found(self, name='graf'):
        return next(((c, e) for f, c, e in st.discover() if f.name == name), None)

    # C1: a second yaml must not make the stack vanish
    def test_second_yaml_keeps_the_stack(self):
        f, base = self.single_yaml_stack()
        self.ui.save_files('graf', {**base, 'files': {'datasources.yaml': 'apiVersion: 1\n'}})
        self.assertEqual(self.found(), ('10-graf.yaml', 'graf.env'))

    def test_restored_yaml_keeps_the_stack(self):
        f, base = self.single_yaml_stack()
        hist = f / '.history'
        hist.mkdir()
        (hist / 'dash.yaml.20261021T100000Z').write_bytes(b'a: 1\n')
        self.ui.restore('graf', {'version': 'dash.yaml.20261021T100000Z'})
        self.assertEqual(self.found(), ('10-graf.yaml', 'graf.env'))

    # I1: a second *.env or a .env stack file must not change the env file
    def test_second_env_keeps_the_env_file(self):
        f, base = self.single_yaml_stack()
        self.ui.save_files('graf', {**base, 'files': {'extra.env': 'A=1\n'}})
        self.assertEqual(self.found(), ('10-graf.yaml', 'graf.env'))

    def test_dot_env_refused_as_stack_file(self):
        f, base = self.single_yaml_stack()
        with self.assertRaises(ValueError):
            self.ui.save_files('graf', {**base, 'files': {'.env': 'A=1\n'}})
        self.assertFalse((f / '.env').exists())

    # I2: Home Assistant YAML tags
    def test_home_assistant_tags(self):
        f, base = self.single_yaml_stack()
        self.ui.save_files('graf', {**base, 'files': {
            'configuration.yaml': 'mqtt:\n  password: !secret mqtt_pw\nautomation: !include automations.yaml\n'
                                  'api_key: !env_var MY_KEY\n'}})
        self.assertTrue((f / 'configuration.yaml').is_file())

    # I3: realistic values that are not secrets
    def test_not_secrets(self):
        f, base = self.single_yaml_stack()
        self.ui.save_files('graf', {**base, 'files': {'ds.yaml': (
            'basicAuthPassword: $GF_PW\n'
            'token: |\n  ${TOKEN}\n'
            'password: >\n  folded\n'
            'secret: null\n'
            'api_key: ~\n')}})
        self.assertTrue((f / 'ds.yaml').is_file())

    def test_quoted_value_with_hash_is_a_secret(self):
        f, base = self.single_yaml_stack()
        with self.assertRaises(ValueError):
            self.ui.save_files('graf', {**base, 'files': {'a.yaml': 'password: "abc #x"\n'}})
        self.assertEqual(self.ui.literal('"abc #x"'), 'abc #x')
        self.assertEqual(self.ui.literal('abc # comment'), 'abc')
