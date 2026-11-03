import importlib

from tests.support import Base, make_stack
from stack_ui import preflight as pf
from stack_ui import server as srv
from stack_ui import stacks as st

NET = '''services:
  app:
    image: docker.io/library/nginx:latest
    networks:
      - proxy
networks:
  proxy:
    name: ${PROXY_NETWORK:-proxy-net}
    external: true
'''


def web(*ports, image='docker.io/library/nginx:latest', extra=''):
    lines = ''.join(f'      - {p}\n' for p in ports)
    return f'services:\n  app:\n    image: {image}\n{extra}' + (f'    ports:\n{lines}' if ports else '')


def holder(name='other', host_port=8080, protocol='tcp', host_ip='', state='running'):
    return {'Names': [name], 'State': state,
            'Ports': [{'host_ip': host_ip, 'host_port': host_port, 'container_port': 80, 'protocol': protocol}]}


class ExternalNetworkTest(Base):
    def test_a_missing_external_network_is_an_error(self):
        r = pf.check(NET, None, [], ['podman'])
        self.assertEqual(len(r['errors']), 1)
        self.assertIn('proxy-net', r['errors'][0])
        self.assertEqual(r['warnings'], [])

    def test_an_existing_external_network_is_fine(self):
        self.assertEqual(pf.check(NET, None, [], ['podman', 'proxy-net'])['errors'], [])

    def test_the_name_comes_from_the_env_file(self):
        r = pf.check(NET, 'PROXY_NETWORK=lan\n', [], ['podman', 'proxy-net'])
        self.assertEqual(len(r['errors']), 1)
        self.assertIn('lan', r['errors'][0])
        self.assertNotIn('proxy-net', r['errors'][0])

    def test_without_a_name_the_key_is_the_name(self):
        r = pf.check('services:\n  a:\n    image: docker.io/x/y:1\nnetworks:\n  lan:\n    external: true\n', None, [], [])
        self.assertIn('lan', r['errors'][0])

    def test_a_network_the_stack_creates_is_not_checked(self):
        text = 'services:\n  a:\n    image: docker.io/x/y:1\nnetworks:\n  back: {}\n'
        self.assertEqual(pf.check(text, None, [], [])['errors'], [])


class PortTest(Base):
    def test_a_port_another_container_holds_is_an_error(self):
        r = pf.check(web('"${HTTP_PORT:-8080}:80"'), None, [holder()], [])
        self.assertEqual(len(r['errors']), 1)
        self.assertIn('8080', r['errors'][0])
        self.assertIn('other', r['errors'][0])

    def test_the_env_file_sets_the_port(self):
        text = web('"${HTTP_PORT:-8080}:80"')
        self.assertEqual(pf.check(text, 'HTTP_PORT=9090\n', [holder()], [])['errors'], [])
        self.assertEqual(len(pf.check(text, 'HTTP_PORT=9090\n', [holder(host_port=9090)], [])['errors']), 1)

    def test_protocol_matters(self):
        self.assertEqual(pf.check(web('"8080:80"'), None, [holder(protocol='udp')], [])['errors'], [])
        self.assertEqual(len(pf.check(web('"7359:7359/udp"'), None, [holder(host_port=7359, protocol='udp')], [])['errors']), 1)

    def test_a_stopped_container_holds_nothing(self):
        self.assertEqual(pf.check(web('"8080:80"'), None, [holder(state='exited')], [])['errors'], [])

    def test_addresses(self):
        mine = web('"192.168.50.3:8080:80"')
        self.assertEqual(pf.check(mine, None, [holder(host_ip='127.0.0.1')], [])['errors'], [])
        self.assertEqual(len(pf.check(mine, None, [holder(host_ip='')], [])['errors']), 1)
        self.assertEqual(len(pf.check(mine, None, [holder(host_ip='192.168.50.3')], [])['errors']), 1)
        self.assertEqual(len(pf.check(web('"8080:80"'), None, [holder(host_ip='127.0.0.1')], [])['errors']), 1)

    def test_long_syntax_and_a_port_without_a_host_port(self):
        long = web(extra='') + '    ports:\n      - target: 80\n        published: 8080\n        protocol: tcp\n'
        self.assertEqual(len(pf.check(long, None, [holder()], [])['errors']), 1)
        self.assertEqual(pf.check(web('"80"', '9000'), None, [holder(host_port=80)], [])['errors'], [])


class ShortImageTest(Base):
    def test_short_names_warn_with_the_full_name(self):
        for image, full in (('jc21/nginx-proxy-manager:latest', 'docker.io/jc21/nginx-proxy-manager'),
                            ('orbforge/orb', 'docker.io/orbforge/orb'), ('nginx', 'docker.io/library/nginx'),
                            ('redis:8-alpine', 'docker.io/library/redis')):
            with self.subTest(image=image):
                r = pf.check(web(image=image), None, [], [])
                self.assertEqual(r['errors'], [])
                self.assertEqual(len(r['warnings']), 1)
                self.assertIn(full, r['warnings'][0])

    def test_qualified_names_do_not(self):
        for image in ('docker.io/jc21/x', 'ghcr.io/a/b:1', 'lscr.io/linuxserver/x', 'localhost/x:dev',
                      'registry:5000/x', '192.168.1.5:5000/x'):
            with self.subTest(image=image):
                self.assertEqual(pf.check(web(image=image), None, [], [])['warnings'], [])

    def test_a_locally_built_image_is_left_alone(self):
        text = web(image='${IMG:-tools-for-gaggimate}:${TAG:-1.3}', extra='    pull_policy: never\n')
        self.assertEqual(pf.check(text, None, [], [])['warnings'], [])

    def test_the_name_may_come_from_a_variable(self):
        r = pf.check(web(image='${IMG:-jc21/x}:latest'), None, [], [])
        self.assertIn('docker.io/jc21/x', r['warnings'][0])


class UnsetVariableTest(Base):
    def test_a_variable_without_a_value_warns(self):
        r = pf.check(web(image='docker.io/x/y:${TAG}'), None, [], [])
        self.assertEqual(r['errors'], [])
        self.assertEqual(len(r['warnings']), 1)
        self.assertIn('TAG', r['warnings'][0])

    def test_defined_defaulted_and_escaped_are_fine(self):
        text = web(image='docker.io/x/y:${TAG}')
        self.assertEqual(pf.check(text, 'TAG=1\n', [], [])['warnings'], [])
        self.assertEqual(pf.check(text, 'export TAG="1"\n', [], [])['warnings'], [])
        self.assertEqual(pf.check(web(image='docker.io/x/y:${TAG:-1}'), None, [], [])['warnings'], [])
        self.assertEqual(pf.check(web(image='docker.io/x/y:1', extra='    command: echo $$HOME\n'), None, [], [])['warnings'], [])

    def test_a_comment_is_not_scanned(self):
        text = '# the tag is ${TAG}\n' + web(image='docker.io/x/y:1')
        self.assertEqual(pf.check(text, None, [], [])['warnings'], [])

    def test_a_variable_is_named_once(self):
        r = pf.check(web('"${P}:80"', image='docker.io/x/y:${P}'), None, [], [])
        self.assertEqual(len([w for w in r['warnings'] if '${P}' in w or ' P ' in w]), 1)

    def test_not_yaml_is_left_to_podman_compose(self):
        self.assertEqual(pf.check('services: [', None, [], []), {'errors': [], 'warnings': []})


class DeployTest(Base):
    def stack(self, compose=NET, env='TZ=Europe/Budapest\n'):
        return make_stack(self.stacks, 'web', compose, env=env)

    def test_plan_lists_the_problems(self):
        f = self.stack()
        p = st.plan(f, 'compose.yaml', '.env')
        self.assertIn('proxy-net', p['errors'][0])
        self.assertEqual(p['warnings'], [])

    def test_a_deploy_with_an_error_stops_before_compose(self):
        f = self.stack()
        with self.assertRaises(st.StackError) as ctx:
            st.deploy(f, 'compose.yaml', '.env', approve=lambda cs: True, log=lambda _: None)
        self.assertIn('proxy-net', str(ctx.exception))
        self.assertEqual(self.compose_calls(), [])

    def test_the_network_makes_it_deploy(self):
        f = self.stack()
        self.mock.networks.append({'name': 'proxy-net', 'driver': 'bridge', 'subnets': []})
        st.deploy(f, 'compose.yaml', '.env', pull=False, approve=lambda cs: True, log=lambda _: None)
        self.assertTrue(self.compose_calls())

    def test_a_warning_does_not_stop_and_is_logged(self):
        f = self.stack(web(image='jc21/x:latest'))
        lines = []
        st.deploy(f, 'compose.yaml', '.env', pull=False, approve=lambda cs: True, log=lines.append)
        self.assertTrue(self.compose_calls())
        self.assertTrue(any('docker.io/jc21/x' in line for line in lines))

    def test_the_stacks_own_containers_do_not_hold_its_ports(self):
        f = self.stack(web('"8080:80"'))
        self.mock.add_container('web', project='web', ports=[{'host_ip': '', 'host_port': 8080, 'container_port': 80,
                                                              'protocol': 'tcp'}])
        self.assertEqual(st.plan(f, 'compose.yaml', '.env')['errors'], [])
        self.mock.add_container('rival', project='rival', ports=[{'host_ip': '', 'host_port': 8080, 'container_port': 80,
                                                                  'protocol': 'tcp'}])
        self.assertIn('rival', st.plan(f, 'compose.yaml', '.env')['errors'][0])

    def test_dry_run_logs_the_problems_and_does_not_raise_for_warnings(self):
        f = self.stack(web(image='jc21/x:latest'))
        lines = []
        st.deploy(f, 'compose.yaml', '.env', dry_run=True, approve=lambda cs: True, log=lines.append)
        self.assertTrue(any('docker.io/jc21/x' in line for line in lines))
        self.assertEqual(self.compose_calls(), [])


class ApiTest(Base):
    def test_the_plan_answer_carries_errors_and_warnings(self):
        ui = importlib.reload(srv)
        make_stack(self.stacks, 'web', NET.replace('docker.io/library/nginx:latest', 'jc21/x:latest'))
        p = ui.plan('web')
        self.assertIn('proxy-net', p['errors'][0])
        self.assertIn('docker.io/jc21/x', p['warnings'][0])
        self.assertEqual(p['missingSecrets'], [])
