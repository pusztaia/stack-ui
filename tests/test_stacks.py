import json
import os

from tests.support import Base, compose_yaml, make_stack
from stack_ui import podman_api as pa
from stack_ui import stacks as st


class DiscoverTest(Base):
    def names(self):
        return {f.name: (c, e) for f, c, e in st.discover()}

    def test_known_compose_names_and_env(self):
        make_stack(self.stacks, 'a', compose_yaml('a'))
        make_stack(self.stacks, 'b', compose_yaml('b'), compose_name='docker-compose.yml', env_name='b.env')
        make_stack(self.stacks, 'c', compose_yaml('c'), env=None, compose_name='01-c.yaml')
        self.assertEqual(self.names(), {'a': ('compose.yaml', '.env'), 'b': ('docker-compose.yml', 'b.env'),
                                        'c': ('01-c.yaml', None)})

    def test_known_name_wins_over_other_yamls(self):
        f = make_stack(self.stacks, 'a', compose_yaml('a'))
        (f / 'override.yml').write_text('services: {}\n', encoding='utf-8')
        self.assertEqual(self.names()['a'][0], 'compose.yaml')

    def test_two_unknown_yamls_need_stack_json(self):
        f = make_stack(self.stacks, 'a', compose_yaml('a'), compose_name='one.yaml')
        (f / 'two.yaml').write_text(compose_yaml('a'), encoding='utf-8')
        self.assertNotIn('a', self.names())
        (f / st.MANIFEST).write_text(json.dumps({'compose': 'two.yaml', 'env': None}), encoding='utf-8')
        self.assertEqual(self.names()['a'], ('two.yaml', None))

    def test_bad_folder_names_skipped(self):
        for name in ('My App', 'Upper', '.hidden', '-x'):
            make_stack(self.stacks, name, compose_yaml('x'))
        make_stack(self.stacks, 'ok_1', compose_yaml('ok'))
        self.assertEqual(list(self.names()), ['ok_1'])

    def test_ignore_list(self):
        make_stack(self.stacks, 'stack-ui', compose_yaml('stack-ui'))
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.patch(st, 'IGNORE', {'stack-ui'})
        self.assertEqual(list(self.names()), ['web'])

    def test_missing_stacks_dir(self):
        self.patch(st, 'STACKS_DIR', self.tmp / 'nope')
        with self.assertRaises(st.StackError):
            st.discover()


class StackOpsTest(Base):
    def stack(self, name='web', secrets=(), env='TZ=Europe/Budapest\n'):
        return make_stack(self.stacks, name, compose_yaml(name, secrets), env=env)

    def test_deploy_without_env_has_no_env_file_argument(self):
        f = self.stack(env=None)
        st.deploy(f, 'compose.yaml', None, pull=False, approve=lambda cs: True, log=lambda _: None)
        self.assertEqual(self.compose_calls()[-1], ['--podman-path', st.PODMAN_PATH, '-p', 'web', '-f', 'compose.yaml', 'up', '-d'])
        self.assertEqual(sorted(p.name for p in (f / st.DEPLOYED).iterdir()), ['compose.yaml'])
        self.assertEqual(st.deployed_diff(f, 'compose.yaml', None, 'host'), ('', ''))

    def test_deploy_pull_then_up_records(self):
        f = self.stack()
        self.assertEqual(st.deploy(f, 'compose.yaml', '.env', approve=lambda cs: True, log=lambda _: None), 'create')
        base = ['--podman-path', st.PODMAN_PATH, '-p', 'web', '-f', 'compose.yaml', '--env-file', '.env']
        self.assertEqual(self.compose_calls(), [base + ['pull'], base + ['up', '-d', '--force-recreate']])

    def test_pull_before_removing_containers_in_the_way(self):
        f = self.stack()
        self.mock.add_container('web')
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'pull'
        with self.assertRaises(st.StackError):
            st.deploy(f, 'compose.yaml', '.env', approve=lambda cs: True, log=lambda _: None)
        self.assertEqual(len(self.mock.containers), 1)

    def test_blocking_needs_approval(self):
        f = self.stack()
        self.mock.add_container('web')
        with self.assertRaises(st.StackError):
            st.deploy(f, 'compose.yaml', '.env', pull=False, approve=lambda cs: False, log=lambda _: None)
        st.deploy(f, 'compose.yaml', '.env', pull=False, approve=lambda cs: True, log=lambda _: None)
        self.assertEqual(self.mock.containers, [])

    def test_missing_secret_refused_before_compose(self):
        f = self.stack(secrets=('tg',))
        with self.assertRaises(st.StackError):
            st.deploy(f, 'compose.yaml', '.env', approve=lambda cs: True, log=lambda _: None)
        self.assertEqual(self.compose_calls(), [])

    def test_compose_failure_keeps_no_record(self):
        f = self.stack()
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'up'
        with self.assertRaises(st.StackError):
            st.deploy(f, 'compose.yaml', '.env', approve=lambda cs: True, log=lambda _: None)
        self.assertFalse((f / st.DEPLOYED).exists())

    def test_stack_actions(self):
        f = self.stack()
        for action, expect in (('stop', 'stop'), ('start', 'start'), ('delete', 'down')):
            st.stack_action(f, 'compose.yaml', '.env', action, log=lambda _: None)
            self.assertEqual(self.compose_calls()[-1][-1], expect)
        with self.assertRaises(ValueError):
            st.stack_action(f, 'compose.yaml', '.env', 'kill')

    def test_check_config(self):
        f = self.stack()
        st.check_config(f, 'compose.yaml', '.env')
        os.environ['FAKE_COMPOSE_FAIL_ON'] = 'config'
        with self.assertRaises(st.StackError):
            st.check_config(f, 'compose.yaml', '.env')

    def test_projects_and_plan(self):
        f = self.stack()
        self.mock.add_container('web', project='web')
        self.mock.add_container('b', labels={'com.docker.compose.project': 'other-tool'})
        self.assertEqual(st.projects(pa.containers()), {'web'})
        self.assertEqual(st.plan(f, 'compose.yaml')['action'], 'update')

    def test_deployed_record_keeps_owner(self):
        chowned = []
        self.patch(st, '_is_root', lambda: True)
        self.patch(st, 'chown', lambda path, uid, gid: chowned.append(os.path.basename(path)))
        f = self.stack()
        st.deploy(f, 'compose.yaml', '.env', pull=False, approve=lambda cs: True, log=lambda _: None)
        self.assertEqual(sorted(chowned), ['.deployed', '.env', 'compose.yaml'])
