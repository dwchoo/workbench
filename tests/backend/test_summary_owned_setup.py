"""Owned setup/collection fixtures only; no OMP, provider or operating-model proof."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.launcher import LaunchPlan, default_models_file, omp_environment
from workbench.backend.summary_adapter import prepare_summary
from workbench.backend.summary_compat_priority import producer
from workbench.contracts.logs_v1 import canonical_json
from workbench.terminal.shell_g2.prototype import ShellChoice


class OwnedSetupTests(unittest.TestCase):
    def test_detached_table_and_managed_role_env_preserve_shipped_table(self):
        with TemporaryDirectory() as name, patch('subprocess.Popen', side_effect=AssertionError('Native forbidden')):
            root = Path(name)
            copied = root / 'runtime/omp_bridge/omp-models.yml'
            copied.parent.mkdir(parents=True)
            shipped = default_models_file().read_bytes()
            copied.write_bytes(shipped)
            table = runtime._owned_model_table(root / 'runtime')
            self.assertEqual(copied.read_bytes(), shipped)
            self.assertEqual(default_models_file().read_bytes(), shipped)
            self.assertEqual(table['worker']['models']['default'], 'ownedverify/worker:high')
            original = deepcopy(table)
            plan = LaunchPlan(ShellChoice('bash', '/bin/bash'), '/owned/omp', '18.8.6', '/owned/entry.ts')
            for role in ('manager', 'worker'):
                env = omp_environment(runtime._environment(root), plan, role=role, token='owned-token',
                    bridge_socket=root / 'absent.sock', models=table)
                self.assertEqual(env['HOME'], str(root / 'home'))
                self.assertFalse(any(key.endswith('API_KEY') for key in env))
                if role == 'worker':
                    self.assertNotIn('WORKBENCH_G3_SUMMARY_SELECTION', env)
                else:
                    selection = json.loads(env['WORKBENCH_G3_SUMMARY_SELECTION'])
                    self.assertEqual(selection['selector'], 'ownedverify/worker:high')
                    self.assertEqual(selection['source'], 'configured_worker_default')
                    self.assertEqual(selection['modelTableDigest'], sha256(canonical_json(table)).hexdigest())
            self.assertEqual(table, original)

    def test_private_catalog_contains_distinct_reasoning_supported_loopback_models(self):
        with TemporaryDirectory() as name, patch('subprocess.Popen', side_effect=AssertionError('Native forbidden')):
            root = Path(name)
            (root / 'agent').mkdir()
            runtime._write_owned_models(root, 'http://127.0.0.1:1234/v1', tokenizer='claude-v3')
            path = root / 'agent/models.yml'
            text = path.read_text()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('auth: none', text)
            blocks = text.split('      - id: ')[1:]
            self.assertEqual([block.splitlines()[0] for block in blocks], ['scripted', 'worker'])
            for block in blocks:
                for line in ('contextWindow: 131072', 'maxTokens: 8192', 'reasoning: true', 'tokenizer: claude-v3'):
                    self.assertIn(line, block)
            self.assertEqual(text.count('baseUrl: http://127.0.0.1:1234/v1'), 1)

    def test_original_three_chunks_keep_exact_selected_plus_omitted_coverage(self):
        with TemporaryDirectory() as name, patch('subprocess.Popen', side_effect=AssertionError('Native forbidden')):
            root = Path(name)
            observations = {'raw_queries': []}
            # Match the existing priority factory's pre-created private data root.
            (root / 'data').mkdir(mode=0o700)
            _layout, _catalog, _sink, _authority, request, pieces, originals = producer(root, observations)
            self.assertEqual([len(piece.text.encode()) for piece in pieces], [4096] * 3)
            model = {'provider': 'ownedverify', 'id': 'scripted', 'api': 'openai-completions',
                'baseUrl': 'http://127.0.0.1:1234/v1', 'contextWindow': 131072, 'maxTokens': 8192, 'tokenizer': 'claude-v3'}
            metadata = {'source': 'parent_session_readback', 'epoch': 'controlled-static-only', 'parentSessionId': 'owned-parent',
                'model': model, 'thinkingLevel': 'off', 'serviceTierByFamily': {},
                'childSelection': {'source': 'configured_worker_default', 'selector': 'ownedverify/worker:high',
                    'modelTableDigest': 'a' * 64, 'model': {**model, 'id': 'worker'}, 'thinkingLevel': 'high',
                    'descriptorSource': 'manager_model_registry_getAll_all', 'serviceTierSource': 'parent_session_readback'},
                'existingContextTokens': None, 'outputCap': 8192, 'reasoningTokens': None, 'visibleResultTokens': None,
                'outputMeaning': 'unknown', 'outputLimitEnforced': None, 'outputLimitSource': 'host_default',
                'estimator': 'utf8_bytes_upper_bound', 'status': 'inconclusive',
                'unknowns': ['current_context_unavailable', 'output_ceiling_enforcement_unavailable', 'reasoning_budget_semantics_unavailable']}
            def count(query):
                self.assertEqual(query['owner']['model']['id'], 'worker')
                return {'version': query['version'], 'ownerDigest': query['ownerDigest'],
                    'fragmentDigests': query['fragmentDigests'], 'encoding': 'ClaudeV3', 'exact': True,
                    'tokens': [100, 100], 'estimator': 'native_known_family_strict'}  # controlled count-port contract only
            prepared = prepare_summary(request, pieces, metadata=metadata, instructions='Owned static instruction', count_fragments=count)
            selected = [span for piece in prepared['selected'] for span in piece.get('ranges', [piece.get('range')])]
            omitted = [piece['range'] for piece in prepared['omitted']]
            self.assertTrue(selected)
            self.assertEqual(sorted(selected + omitted, key=lambda span: span['start']), originals)
            self.assertEqual(len({canonical_json(span) for span in selected + omitted}), 3)
            self.assertEqual(prepared['metadata'], metadata)
            self.assertEqual(prepared['budget']['outputReserve'], 8192)
            print('controlled count fixture selected=' + str(len(selected)) + ' omitted=' + str(len(omitted)) + '; Native count/selection unmeasured')
