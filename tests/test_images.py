import importlib
from tests.support import Base, compose_yaml, make_stack
from stack_ui import podman_api as pa, server as srv, stacks as st


class ImagesTest(Base):
    def setUp(self):
        super().setUp()
        self.ui = importlib.reload(srv)
        make_stack(self.stacks, 'web', compose_yaml('web'))
        self.mock.add_container('web', project='web', image='docker.io/library/nginx:latest', image_id='used1')
        self.mock.images += [
            {'Id': 'used1', 'RepoTags': ['docker.io/library/nginx:latest'],
             'RepoDigests': ['docker.io/library/nginx@sha256:aaa'], 'Size': 100, 'Created': 2},
            {'Id': 'old1', 'RepoTags': None, 'RepoDigests': [], 'Size': 50, 'Created': 1},
        ]

    def test_images_from_podman(self):
        data = self.ui.images()
        rows = {r['id']: r for r in data['images']}
        self.assertEqual(rows['used1']['usedBy'], ['web'])
        self.assertTrue(rows['old1']['removable'])
        self.assertIsNone(rows['old1']['endpoint'])
        self.assertEqual(data['reclaimable'], 50)

    def test_prune_removes_only_removable(self):
        r = self.ui.prune({'ids': ['old1', 'used1']})
        self.assertEqual((r['removed'], r['freed']), (['old1'], 50))
        self.assertEqual([i['Id'] for i in self.mock.images], ['used1'])

    def test_updates_compare_digests(self):
        self.patch(self.ui, 'check_ref', lambda ref: ('sha256:bbb', ''))
        data = self.ui.updates(check=True)
        self.assertEqual(data['stacks']['web']['status'], 'update')
        self.patch(self.ui, 'check_ref', lambda ref: ('sha256:aaa', ''))
        self.assertEqual(self.ui.updates(check=True)['stacks']['web']['status'], 'current')
