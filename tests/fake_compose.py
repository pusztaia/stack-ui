"""Stands in for podman-compose in the tests: records its arguments and working directory, prints a line, and
exits 3 when FAKE_COMPOSE_FAIL_ON names one of its arguments (e.g. "up")."""
import json
import os
import sys

log = os.environ.get('FAKE_COMPOSE_LOG')
if log:
    with open(log, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\n')
print('fake podman-compose', ' '.join(sys.argv[1:]))
fail_on = os.environ.get('FAKE_COMPOSE_FAIL_ON', '')
sys.exit(3 if fail_on and fail_on in sys.argv[1:] else 0)
