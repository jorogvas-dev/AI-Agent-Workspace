#!/usr/bin/env python3
"""Fixed, approved job-offer workflow for the isolated Python executor.

Run only after the knowledge token file is mounted read-only inside the
executor. No token is accepted as a tool argument, printed, or saved in state.
The model selects this fixed script by filename, not a plan or command string.
"""
from __future__ import annotations

import importlib.util
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

WORKSPACE = Path('/workspace')
SCRIPTS = WORKSPACE / 'scripts'
CONTROLLER = SCRIPTS / 'task_controller_v02.py'
PLAN = SCRIPTS / 'job_offer_live_plan.json'
SECRET_FILE = Path(os.environ.get(
    'KNOWLEDGE_TOKEN_FILE',
    '/run/secrets/agent_knowledge_token',
))
KNOWLEDGE_SEARCH = 'http://knowledge-service:8002/search'


def fetch_knowledge(query: str, limit: int) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Invalid knowledge search query')
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('Invalid search limit')
    if not SECRET_FILE.is_file():
        raise RuntimeError('Knowledge credential mount missing')
    token = SECRET_FILE.read_text(encoding='utf-8').strip()
    if not token:
        raise RuntimeError('Knowledge credential file is empty')
    payload = json.dumps({'query': query, 'limit': limit}).encode('utf-8')
    req = urllib.request.Request(
        KNOWLEDGE_SEARCH, data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            result = json.loads(res.read(1_000_001))
    except urllib.error.HTTPError as exc:
        # Deliberately avoid echoing server body or credentials.
        raise RuntimeError(f'Knowledge search failed with HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError('Knowledge search request failed') from exc
    if not isinstance(result, dict) or result.get('status') != 'success' or not isinstance(result.get('results'), list):
        raise RuntimeError('Knowledge service returned an invalid result')
    return result


def load_controller():
    if not CONTROLLER.is_file():
        raise RuntimeError('Missing task_controller_v02.py')
    spec = importlib.util.spec_from_file_location('task_controller_v02', CONTROLLER)
    if spec is None or spec.loader is None:
        raise RuntimeError('Unable to load task controller')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _redact(message: str, token: str) -> str:
    text = str(message)
    return text.replace(token, '[REDACTED]') if token else text


def _structured_result(plan, state, root: Path, token: str) -> dict:
    steps = state.get('steps', [])
    completed_steps = [
        {
            'number': step.get('number'),
            'type': step.get('type'),
        }
        for step in steps
        if step.get('status') == 'completed'
    ]

    verified_output_path = None
    file_sha256 = None

    for step in plan['steps']:
        if step.get('type') != 'verify_file':
            continue

        matching = next(
            (
                item for item in steps
                if item.get('type') == 'verify_file'
                and item.get('status') == 'completed'
                and item.get('number') == plan['steps'].index(step) + 1
            ),
            None,
        )
        if matching is None:
            continue

        target = controller_safe_target(root, step.get('path'))
        data = target.read_bytes()
        verified_output_path = '/workspace/' + str(
            target.relative_to(root)
        ).replace('\\', '/')
        file_sha256 = hashlib.sha256(data).hexdigest()
        break

    success = state.get('status') == 'completed' and file_sha256 is not None
    error = None
    if not success:
        failed = next(
            (step for step in steps if step.get('status') == 'failed'),
            None,
        )
        error = _redact(
            (failed or {}).get('error', 'Workflow failed'),
            token,
        )

    return {
        'success': success,
        'completed_steps': completed_steps,
        'verified_output_path': verified_output_path,
        'file_sha256': file_sha256,
        'error': error,
    }


def controller_safe_target(root: Path, relative_path: str) -> Path:
    controller = load_controller()
    return controller.safe_target(root, relative_path)


def run_workflow():
    token = ''
    try:
        if SECRET_FILE.is_file():
            token = SECRET_FILE.read_text(encoding='utf-8').strip()

        controller = load_controller()
        if not PLAN.is_file():
            raise RuntimeError('Missing approved job offer plan')
        plan = json.loads(PLAN.read_text(encoding='utf-8'))
        if not isinstance(plan, dict) or not isinstance(plan.get('facts'), dict) or not isinstance(plan.get('steps'), list):
            raise RuntimeError('Approved plan has an invalid structure')
        if not plan['steps'] or len(plan['steps']) > 25:
            raise RuntimeError('Approved plan has an invalid number of steps')
        if any(not isinstance(step, dict) or step.get('type') not in controller.SUPPORTED for step in plan['steps']):
            raise RuntimeError('Approved plan has an unsupported step')
        if not (WORKSPACE / 'output').is_dir() or not (WORKSPACE / 'temp').is_dir():
            raise RuntimeError('Workspace output/temp mounts missing')

        controller.fetch_knowledge = fetch_knowledge
        digest = controller.sha256(str(PLAN).encode())[:12]
        state_file = WORKSPACE / 'temp' / f'task-state-{digest}.json'
        state = {'plan': str(PLAN), 'workspace': str(WORKSPACE)}

        # The controller's progress output is operational detail, not the
        # executor API result. Keep stdout reserved for one JSON object.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return_code = controller.execute(plan, WORKSPACE, state, state_file)

        if state_file.is_file():
            state = json.loads(state_file.read_text(encoding='utf-8'))

        result = _structured_result(plan, state, WORKSPACE, token)
        if return_code != 0:
            result['success'] = False
        return result
    except Exception as exc:
        return {
            'success': False,
            'completed_steps': [],
            'verified_output_path': None,
            'file_sha256': None,
            'error': _redact(str(exc), token),
        }


def main():
    result = run_workflow()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
