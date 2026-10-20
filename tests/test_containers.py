import importlib
import json

from tests.support import Base, compose_yaml, make_stack
from stack_ui import podman_api as pa, server as srv, stacks as st


class ContainersTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        self.patch(self.ui, 'SELF', 'stack-ui')
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.mock.add_container('web', project='web', ports=[{'host_ip': '', 'host_port': 8080, 'container_port': 80,
                                                              'protocol': 'tcp'}, {'host_ip': '', 'host_port': 0,
                                                              'container_port': 9000, 'protocol': 'tcp'}], created=1760000000)
        self.mock.add_container('orphan', project='gone')
        self.mock.add_container('stack-ui', project='stack-ui')

    def rows(self):
        return {r['name']: r for r in self.ui.container_table()['containers']}

    def test_table_marks_stack_and_protected(self):
        rows = self.rows()
        self.assertEqual((rows['web']['inStack'], rows['web']['stack'], rows['web']['protected']), (True, 'web', False))
        self.assertEqual((rows['orphan']['inStack'], rows['orphan']['stack']), (False, 'gone'))
        self.assertTrue(rows['stack-ui']['protected'])
        self.assertEqual(rows['web']['ports'], [{'host': '', 'port': 8080, 'container': 80, 'protocol': 'tcp'}])
        self.assertEqual(rows['web']['created'], 1760000000)

    def test_actions(self):
        self.ui.container_do({'name': 'web', 'action': 'stop'})
        self.assertEqual(self.mock.containers[0]['State'], 'exited')
        self.ui.container_do({'name': 'web', 'action': 'start'})
        self.ui.container_do({'name': 'web', 'action': 'restart'})
        self.assertIn(('POST', '/containers/id-web/restart', {'t': '30'}), self.mock.calls)

    def test_remove_outside_stack(self):
        self.ui.container_do({'name': 'orphan', 'action': 'remove'})
        self.assertNotIn('orphan', self.rows())

    def test_remove_refused_for_stack_container(self):
        with self.assertRaises(ValueError):
            self.ui.container_do({'name': 'web', 'action': 'remove'})
        self.assertIn('web', self.rows())

    def test_protected_container_cannot_be_stopped_or_restarted(self):
        for action in ('stop', 'restart', 'remove'):
            with self.assertRaises(ValueError):
                self.ui.container_do({'name': 'stack-ui', 'action': action})
        self.assertEqual(self.rows()['stack-ui']['state'], 'running')

    def test_unknown_name_or_action(self):
        for data in ({'name': 'nope', 'action': 'stop'}, {'name': 'web', 'action': 'kill'}):
            with self.assertRaises(ValueError):
                self.ui.container_do(data)

    def test_details_masks_command_line_and_wider_names(self):
        c = self.mock.add_container('app2', env=['SECRET_KEY=Zq1', 'AWS_SECRET_ACCESS_KEY=Zq2',
                                                  'DATABASE_URL=postgres://u:Zq3@db/x', 'TZ=UTC'])
        c['MockCreate'] = ['podman', 'create', '-e', 'DB_PASSWORD=Zq4', '--env', 'API_TOKEN=Zq5', '--name', 'app2', 'img']
        c['MockCmd'] = ['--password', 'Zq6', '--admin-token=Zq7', '--verbose']
        info = self.ui.container_details('app2')
        self.assertNotIn('Zq', json.dumps(info))
        self.assertIn('TZ=UTC', info['Config']['Env'])
        self.assertIn('DATABASE_URL=postgres://u:••••@db/x', info['Config']['Env'])
        self.assertEqual(info['Args'], ['--password', '••••', '--admin-token=••••', '--verbose'])

    def test_details_masks_secret_env(self):
        self.mock.add_container('app', env=['TZ=UTC', 'API_KEY=abc', 'telegram_token=xyz', 'DB_PASSWORD=', 'NOEQUALS'])
        env = self.ui.container_details('app')['Config']['Env']
        self.assertEqual(env, ['TZ=UTC', 'API_KEY=••••', 'telegram_token=••••', 'DB_PASSWORD=••••', 'NOEQUALS'])
        with self.assertRaises(ValueError):
            self.ui.container_details('nope')


class ImagesManageTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.mock.add_container('web', project='web', image_id='used1')
        self.mock.images += [
            {'Id': 'used1', 'RepoTags': ['docker.io/library/nginx:latest'], 'RepoDigests': ['docker.io/library/nginx@sha256:a'],
             'Size': 100, 'Created': 2, 'Architecture': 'arm64', 'Os': 'linux', 'Labels': {'x': '1'},
             'RootFS': {'Layers': ['l1', 'l2']}},
            {'Id': 'free1', 'RepoTags': ['docker.io/library/alpine:3', 'localhost/alp:keep'], 'RepoDigests': [], 'Size': 50, 'Created': 1},
        ]

    def test_pull(self):
        self.ui.image_pull({'reference': 'docker.io/library/redis:7'})
        self.assertEqual(self.mock.pulled, ['docker.io/library/redis:7'])

    def test_pull_error_and_bad_reference(self):
        self.mock.pull_errors['docker.io/library/nope:1'] = 'manifest unknown'
        with self.assertRaises(pa.PodmanError):
            self.ui.image_pull({'reference': 'docker.io/library/nope:1'})
        for bad in ('', 'Docker.io/x', 'x y', 'x;rm -rf /'):
            with self.assertRaises(ValueError):
                self.ui.image_pull({'reference': bad})
        self.assertEqual(self.mock.pulled, [])

    def test_tag(self):
        self.ui.image_tag({'id': 'free1', 'repo': 'localhost/alpine', 'tag': 'pinned'})
        self.assertIn('localhost/alpine:pinned', self.mock.images[1]['RepoTags'])
        for bad in ({'id': 'free1', 'repo': 'localhost/alpine', 'tag': 'bad tag'},
                    {'id': 'free1', 'repo': 'BAD', 'tag': 'x'}, {'id': 'nope', 'repo': 'localhost/a', 'tag': 'x'}):
            with self.assertRaises(ValueError):
                self.ui.image_tag(bad)

    def test_remove_unused_image_without_force(self):
        r = self.ui.image_remove({'id': 'free1'})
        self.assertEqual((r['removed'], r['freed']), ('free1', 50))
        self.assertEqual([i['Id'] for i in self.mock.images], ['used1'])
        deletes = [(path, q) for m, path, q in self.mock.calls if m == 'DELETE']
        self.assertTrue(deletes)
        self.assertTrue(all(q.get('force') != 'true' for _, q in deletes))     # Podman refuses if a container appeared

    def test_remove_refused_for_used_image(self):
        with self.assertRaises(ValueError):
            self.ui.image_remove({'id': 'used1'})
        self.assertEqual([i['Id'] for i in self.mock.images], ['used1', 'free1'])

    def test_details(self):
        d = self.ui.image_details('used1')
        self.assertEqual((d['architecture'], d['os'], d['layers'], d['usedBy']), ('arm64', 'linux', 2, ['web']))
        self.assertEqual(d['digests'], ['docker.io/library/nginx@sha256:a'])
        with self.assertRaises(ValueError):
            self.ui.image_details('nope')
