import unittest

from tests.support import Base, fresh_server


def sample(route) -> list[str]:
    """A request path for the route: its parts after /api/, a {placeholder} filled with a plain value."""
    return ['api'] + [p.strip('{}') + '1' if p.startswith('{') else p for p in route.path]


class RouteTableTest(Base):
    """The /api/ routes are one table: each line says the method, the path, what it runs, and whether the scripts'
    API token may use it."""

    def setUp(self):
        super().setUp()
        self.ui = fresh_server()

    def test_every_route_is_reached_by_its_own_path(self):
        seen = set()
        for route in self.ui.ROUTES:
            key = (route.method, route.path)
            self.assertNotIn(key, seen, f'two lines for {key}')
            seen.add(key)
            found, args = self.ui.find_route(route.method, sample(route))
            self.assertIs(found, route, f'{route.method} /{"/".join(sample(route))} reaches another line')
            self.assertEqual(len(args), sum(p.startswith('{') for p in route.path))

    def test_a_trailing_slash_or_an_unknown_path_finds_nothing_else(self):
        self.assertEqual(self.ui.find_route('GET', 'api/stacks/'.strip('/').split('/'))[0].path, ('stacks',))
        self.assertIsNone(self.ui.find_route('GET', ['api', 'nosuch'])[0])
        self.assertIsNone(self.ui.find_route('DELETE', ['api', 'stacks'])[0])
        self.assertIsNone(self.ui.find_route('GET', ['api', 'stacks', 'web', 'deploy'])[0])     # POST only
        self.assertIsNone(self.ui.find_route('GET', ['api', 'stacks', '', 'diff'])[0])          # an empty name

    def test_the_routes_the_api_token_may_not_use(self):
        page_only = sorted(f'{r.method} {"/".join(r.path)}' for r in self.ui.ROUTES if r.page_only)
        self.assertEqual(page_only, [
            'GET backup', 'GET backup/download', 'GET schedule', 'GET settings', 'POST backup/save',
            'POST schedule', 'POST schedule/ack', 'POST schedule/run',
            'POST settings/token', 'POST stacks/auto', 'POST stacks/{name}/auto'])


if __name__ == '__main__':
    unittest.main()
