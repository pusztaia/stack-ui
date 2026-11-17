"""A real Podman's answers as a test fixture: only the fields the page reads, renamed (nothing of the host leaks).

    python tools/podman-sample.py stacks/_podman-sample tests/fixtures/podman-5.7

The first folder holds what the Pi answered (podman-version.json, podman-containers.json, podman-stats.json,
podman-inspect.jsonl: CLAUDE.md, Tests, says how to take them); it stays out of the repository (stacks/ is ignored).
Names become c1.., stacks s1.., ids are made up, ports are moved to 20000+ (ranges kept), a host address becomes
192.0.2.10 (a documentation address), images become example images. What the checks depend on (states, health,
restart counts, times, exit codes, memory numbers, OOMKilled) is kept as Podman gave it. Standard library only."""
import hashlib
import json
import sys
from pathlib import Path

if len(sys.argv) != 3:
    sys.exit(__doc__)
RAW, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

containers = json.loads((RAW / 'podman-containers.json').read_text(encoding='utf-8'))
stats = json.loads((RAW / 'podman-stats.json').read_text(encoding='utf-8'))
inspect = [json.loads(x) for x in (RAW / 'podman-inspect.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
version = json.loads((RAW / 'podman-version.json').read_text(encoding='utf-8'))

names, ids, stacks, ports = {}, {}, {}, {}
for i, c in enumerate(sorted(containers, key=lambda c: c['Names'][0]), 1):
    names[c['Names'][0]] = f'c{i}'
    ids[c['Id']] = hashlib.sha256(f'container {i}'.encode()).hexdigest()


def stack_of(label):
    if not label:
        return label
    return stacks.setdefault(label, f's{len(stacks) + 1}')


def port_of(n):
    return ports.setdefault(n, 20000 + 10 * len(ports))


def host(ip):
    return ip if ip in ('', '0.0.0.0', '::') else '192.0.2.10'


out_containers = []
for c in containers:
    labels = c.get('Labels') or {}
    project = labels.get('io.podman.compose.project') or labels.get('com.docker.compose.project') or ''
    keep_labels = {}
    if project:
        keep_labels['io.podman.compose.project'] = keep_labels['com.docker.compose.project'] = stack_of(project)
        if labels.get('io.podman.compose.service'):
            keep_labels['io.podman.compose.service'] = 'app'
    out_containers.append({
        'Id': ids[c['Id']], 'Names': [names[c['Names'][0]]], 'Image': f'docker.io/library/example{len(out_containers) + 1}:latest',
        'State': c['State'], 'Status': c['Status'], 'Restarts': c['Restarts'], 'StartedAt': c['StartedAt'],
        'ExitCode': c['ExitCode'], 'ExitedAt': c['ExitedAt'], 'Exited': c['Exited'], 'Labels': keep_labels,
        'Ports': [{'host_ip': host(p.get('host_ip') or ''), 'host_port': port_of(p['host_port']) if p.get('host_port') else 0,
                   'container_port': port_of(('c', p['container_port'])), 'protocol': p.get('protocol') or 'tcp',
                   'range': p.get('range') or 1} for p in c.get('Ports') or []],
    })

by_id = {c['Id']: c['Names'][0] for c in containers}
out_stats = {'Error': stats.get('Error'), 'Stats': [
    {'ContainerID': ids[s['ContainerID']], 'Name': names[s['Name']], 'MemUsage': s['MemUsage'], 'MemLimit': s['MemLimit'],
     'CPUNano': s['CPUNano'], 'SystemNano': s['SystemNano'], 'PIDs': s['PIDs']}
    for s in stats.get('Stats') or []]}

out_inspect = [{'Id': ids[x['Id']], 'Name': names[x['Name'].lstrip('/')], 'RestartCount': x.get('RestartCount'),
                'State': {k: x['State'].get(k) for k in ('Status', 'Running', 'Restarting', 'OOMKilled', 'ExitCode',
                                                         'StartedAt', 'FinishedAt')}}
               for x in inspect]

(OUT / 'containers.json').write_bytes((json.dumps(out_containers, indent=1) + '\n').encode('utf-8'))
(OUT / 'stats.json').write_bytes((json.dumps(out_stats, indent=1) + '\n').encode('utf-8'))
(OUT / 'inspect.json').write_bytes((json.dumps(out_inspect, indent=1) + '\n').encode('utf-8'))
(OUT / 'version.json').write_bytes((json.dumps({'Version': version['Version'], 'ApiVersion': version['ApiVersion']}) + '\n').encode('utf-8'))
print('written', len(out_containers), 'containers')
