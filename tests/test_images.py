import importlib
import json
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
             'RepoDigests': ['docker.io/library/nginx@sha256:aaa'], 'Size': 100, 'Created': 2,
             'Os': 'linux', 'Architecture': 'arm64'},
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

    def add_free_images(self):
        self.mock.images += [
            {'Id': 'free1', 'RepoTags': ['localhost/alpine:3', 'localhost/alpine:latest'], 'RepoDigests': [], 'Size': 30, 'Created': 3},
            {'Id': 'free2', 'RepoTags': ['localhost/busybox:1'], 'RepoDigests': [], 'Size': 20, 'Created': 4},
        ]

    def test_several_images_are_removed_at_once(self):
        self.add_free_images()
        r = self.ui.image_remove({'ids': ['free1', 'free2', 'old1']})
        self.assertEqual((sorted(r['removed']), r['freed'], r['errors']), (['free1', 'free2', 'old1'], 100, []))
        self.assertEqual([i['Id'] for i in self.mock.images], ['used1'])
        deletes = [q for m, path, q in self.mock.calls if m == 'DELETE']
        self.assertTrue(deletes)
        self.assertTrue(all(q.get('force') != 'true' for q in deletes))     # never force: Podman refuses a used one

    def test_a_used_or_unknown_image_is_skipped_and_named_the_rest_still_go(self):
        self.add_free_images()
        r = self.ui.image_remove({'ids': ['used1', 'free2', 'nope', 'free2']})
        self.assertEqual((r['removed'], r['freed']), (['free2'], 20))
        self.assertEqual(len(r['errors']), 2)
        self.assertIn('docker.io/library/nginx:latest: used by web', r['errors'])
        self.assertTrue(any(e.startswith('nope: no such image') for e in r['errors']))
        self.assertEqual(sorted(i['Id'] for i in self.mock.images), ['free1', 'old1', 'used1'])

    def test_one_that_podman_refuses_is_reported_and_the_others_go(self):
        self.add_free_images()
        real = self.ui.pa.remove_image

        def refuse(name, force=False):
            if name == 'localhost/busybox:1':      # the image is removed by its name
                raise pa.PodmanError('image is in use by a container', 409)
            return real(name, force)
        self.patch(self.ui.pa, 'remove_image', refuse)
        r = self.ui.image_remove({'ids': ['free2', 'old1']})
        self.assertEqual(r['removed'], ['old1'])
        self.assertEqual(r['errors'], ['localhost/busybox:1: image is in use by a container'])

    def test_bad_ids_are_refused(self):
        for bad in ({'ids': []}, {'ids': 'free1'}, {'ids': [1]}, {'ids': None}):
            with self.assertRaises(ValueError):
                self.ui.image_remove(bad)
        self.assertEqual(len(self.mock.images), 2)

    def test_the_single_form_is_unchanged(self):
        self.add_free_images()
        r = self.ui.image_remove({'id': 'free2'})
        self.assertEqual(r, {'removed': 'free2', 'freed': 20})

    def test_updates_compare_digests(self):
        self.patch(self.ui, 'check_ref', lambda ref, local, platform: (('sha256:bbb',), ''))
        data = self.ui.updates(check=True)
        self.assertEqual(data['stacks']['web']['status'], 'update')
        self.patch(self.ui, 'check_ref', lambda ref, local, platform: (('sha256:aaa',), ''))
        self.assertEqual(self.ui.updates(check=True)['stacks']['web']['status'], 'current')

    def test_local_image_with_a_digest_is_built_here(self):
        # podman gives an image built on the host a digest too (localhost/name@sha256:...): it is still "built
        # here", never a failed registry check
        make_stack(self.stacks, 'tools', compose_yaml('tools'))
        self.mock.add_container('tools', project='tools', image='localhost/tools:1.3-beta', image_id='built1')
        self.mock.images.append({'Id': 'built1', 'RepoTags': ['localhost/tools:1.3-beta'],
                                 'RepoDigests': ['localhost/tools@sha256:loc'], 'Size': 10, 'Created': 3,
                                 'Os': 'linux', 'Architecture': 'arm64'})
        self.patch(self.ui, 'check_ref', lambda ref, local, platform: (('sha256:aaa',), '') if 'nginx' in ref
                   else self.fail(f'{ref} must not be asked of a registry'))
        tools = self.ui.updates(check=True)['stacks']['tools']
        self.assertEqual(tools['status'], 'local')
        self.assertEqual(tools['images'][0]['message'], 'built here: not checked')

    def registry(self, index_digest: str, arm64_digest: str) -> list[str]:
        """A registry serving a multi-arch index for every tag; the methods it was asked with."""
        index = {'manifests': [
            {'digest': 'sha256:amd', 'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'digest': arm64_digest, 'platform': {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}},
            {'digest': 'sha256:att', 'platform': {'os': 'unknown', 'architecture': 'unknown'}}]}
        calls = []

        def fetch(url, token, method):
            calls.append(method)
            return index_digest, self.ui.INDEX_TYPES[0], json.dumps(index).encode() if method == 'GET' else b''
        self.patch(self.ui, 'registry_fetch', fetch)
        return calls

    def test_new_index_with_the_same_platform_image_is_current(self):
        calls = self.registry('sha256:new-index', 'sha256:aaa')
        self.assertEqual(self.ui.updates(check=True)['stacks']['web']['status'], 'current')
        self.assertEqual(calls, ['HEAD', 'GET'])

    def test_new_platform_image_is_an_update(self):
        self.registry('sha256:new-index', 'sha256:new-arm64')
        self.assertEqual(self.ui.updates(check=True)['stacks']['web']['status'], 'update')

    def test_matching_index_needs_no_get(self):
        self.mock.images[0]['RepoDigests'].append('docker.io/library/nginx@sha256:index')
        calls = self.registry('sha256:index', 'sha256:aaa')
        self.assertEqual(self.ui.updates(check=True)['stacks']['web']['status'], 'current')
        self.assertEqual(calls, ['HEAD'])
