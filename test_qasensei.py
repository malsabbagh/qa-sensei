"""Independent public-CLI bootstrap controls; no vendor calls or product fix."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).with_name('qasensei.py')
PYTHON = str(Path(sys.executable).resolve())
CHECKER = '''import json, os, sys, time
mode = sys.argv[1]
if mode == 'timeout': time.sleep(5)
if mode == 'noise':
    while True: print('x' * 65536, flush=True)
if mode == 'missing':
    print('no observation')
elif mode == 'duplicate':
    print('{"version":1,"status":"passed","status":"failed","observed":{},"effects":[]}')
else:
    if mode == 'mutate':
        with open(__file__, 'a') as stream: stream.write('# changed during check\\n')
    if mode == 'secret': print('Authorization: Bearer secret-example-value', file=sys.stderr)
    observation = {'answer': 41 if mode == 'wrong' else 1 if mode == 'one' else 42}
    print(json.dumps({'version':1,'status': mode if mode in ('skipped','flaky','blocked','not_run') else 'passed', 'observed':observation,'effects':[]}))
    if mode == 'exit': sys.exit(3)
'''


class PublicCLIControls(unittest.TestCase):
    def test_canonical_cli_help(self):
        result = self.cli('--help')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('usage: qa-sensei ', result.stdout)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'candidate'
        self.repo.mkdir()
        (self.repo / 'check.py').write_text(CHECKER)
        self.git('init', '-q')
        self.git('add', 'check.py')
        self.git('-c', 'user.name=QA', '-c', 'user.email=qa@localhost', 'commit', '-qm', 'fixture')
        self.head = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def cli(self, *args):
        return subprocess.run([PYTHON, str(CLI), *map(str, args)], capture_output=True, text=True, timeout=15)

    def plan(self, mode='passed', **overrides):
        check = {'id':'C1', 'required':True,
                 'argv':[PYTHON, str(self.repo / 'check.py'), mode],
                 'expected':{'answer':42}, 'fixtures':['check.py'], 'timeout_seconds':1}
        check.update(overrides)
        spec = {'version':1, 'workflow_id':'bootstrap', 'repository':str(self.repo),
                'candidate_commit':self.head, 'checks':[check]}
        spec_path, plan_path = self.root/'spec.json', self.root/'plan.json'
        spec_path.write_text(json.dumps(spec))
        result = self.cli('plan', '--spec', spec_path, '--out', plan_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return plan_path, hashlib.sha256(plan_path.read_bytes()).hexdigest()

    def run_check(self, mode='passed', **overrides):
        plan, digest = self.plan(mode, **overrides)
        bundle = self.root/'bundle'
        result = self.cli('run', '--plan', plan, '--plan-sha256', digest, '--out', bundle)
        self.assertTrue((bundle/'receipt.json').exists(), result.stdout+result.stderr)
        return result, bundle, json.loads((bundle/'receipt.json').read_text())

    def test_known_pass_and_fresh_process_report(self):
        result, bundle, receipt = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(receipt['verdict'], 'passed')
        self.assertEqual(receipt['checks'][0]['status'], 'passed')
        self.assertEqual(receipt['checks'][0]['observed'], {'answer':42})
        report = self.cli('report', '--bundle', bundle)
        self.assertEqual(report.returncode, 0, report.stdout+report.stderr)
        self.assertEqual(self.cli('gate','--bundle',bundle).returncode,0)

    def test_shared_candidate_export_and_tamper_control(self):
        _, bundle, receipt = self.run_check()
        candidate=self.root/'candidate.json'
        result=self.cli('candidate','--plan',self.root/'plan.json','--base-commit',self.head,'--out',candidate)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        export=self.cli('export','--bundle',bundle,'--candidate',candidate,'--task-id','qa','--suite','bootstrap')
        self.assertEqual(export.returncode,0,export.stdout+export.stderr)
        value=json.loads(export.stdout)
        self.assertEqual(value['schema'],'sensei.qa/1.0')
        self.assertEqual(value['candidate'],json.loads(candidate.read_text()))
        self.assertEqual(value['checks'][0]['outcome'],'passed')
        self.assertTrue(value['checks'][0]['check_spec_sha256'])
        self.assertEqual(hashlib.sha256((bundle/'receipt.json').read_bytes()).hexdigest(),value['native_receipt']['sha256'])
        changed=json.loads(candidate.read_text())
        changed['fixture_sha256']='0'*64
        candidate.write_text(json.dumps(changed))
        self.assertNotEqual(self.cli('export','--bundle',bundle,'--candidate',candidate,'--task-id','qa','--suite','bootstrap').returncode,0)

    def test_json_bool_is_not_integer_acceptance(self):
        _, _, receipt=self.run_check('one', expected={'answer':True})
        self.assertEqual(receipt['verdict'],'failed')

    def test_nonfinite_input_is_rejected(self):
        path=self.root/'invalid.json'
        path.write_text('{"version":NaN}')
        self.assertNotEqual(self.cli('plan','--spec',path,'--out',self.root/'plan.json').returncode,0)

    def test_external_checker_script_is_refused(self):
        external=self.root/'outside.py'
        external.write_text(CHECKER)
        spec={'version':1,'workflow_id':'bootstrap','repository':str(self.repo),'candidate_commit':self.head,'checks':[{'id':'C1','required':True,'argv':[PYTHON,str(external),'passed'],'expected':{'answer':42},'fixtures':['check.py'],'timeout_seconds':1}]}
        path=self.root/'outside-spec.json'
        path.write_text(json.dumps(spec))
        self.assertNotEqual(self.cli('plan','--spec',path,'--out',self.root/'plan.json').returncode,0)

    def test_known_fail_even_with_zero_exit(self):
        result, _, receipt = self.run_check('wrong')
        self.assertEqual(result.returncode, 0, 'Protocol completion is separate from product verdict.')
        self.assertEqual(receipt['verdict'], 'failed')
        self.assertEqual(receipt['checks'][0]['status'], 'failed')

    def test_nonzero_process_cannot_pass(self):
        _, _, receipt = self.run_check('exit')
        self.assertNotEqual(receipt['verdict'], 'passed')

    def test_missing_observation_is_blocked(self):
        _, _, receipt = self.run_check('missing')
        self.assertEqual(receipt['checks'][0]['status'], 'blocked')
        self.assertNotEqual(receipt['verdict'], 'passed')

    def test_duplicate_observation_keys_are_blocked(self):
        _, _, receipt = self.run_check('duplicate')
        self.assertEqual(receipt['checks'][0]['status'], 'blocked')

    def test_skip_flaky_not_run_remain_distinct(self):
        for status in ('skipped','flaky','not_run'):
            with self.subTest(status=status):
                plan, digest = self.plan(status)
                bundle = self.root/status
                self.cli('run','--plan',plan,'--plan-sha256',digest,'--out',bundle)
                receipt = json.loads((bundle/'receipt.json').read_text())
                self.assertEqual(receipt['checks'][0]['status'], status)
                self.assertNotEqual(receipt['verdict'], 'passed')
                plan.unlink()

    def test_timeout_and_capture_limit_are_bounded(self):
        for mode in ('timeout','noise'):
            with self.subTest(mode=mode):
                plan, digest = self.plan(mode)
                bundle = self.root/mode
                self.cli('run','--plan',plan,'--plan-sha256',digest,'--out',bundle)
                receipt = json.loads((bundle/'receipt.json').read_text())
                self.assertEqual(receipt['checks'][0]['status'], 'blocked')
                self.assertNotEqual(receipt['verdict'], 'passed')
                plan.unlink()

    def test_wrong_reviewed_plan_digest_refuses_before_execution(self):
        plan, _ = self.plan()
        result = self.cli('run','--plan',plan,'--plan-sha256','0'*64,'--out',self.root/'bundle')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'bundle'/'receipt.json').exists())

    def test_changed_candidate_refuses_before_execution(self):
        plan, digest = self.plan()
        (self.repo/'check.py').write_text('raise RuntimeError("changed")\n')
        result = self.cli('run','--plan',plan,'--plan-sha256',digest,'--out',self.root/'bundle')
        self.assertNotEqual(result.returncode, 0)

    def test_untracked_candidate_content_refuses_before_execution(self):
        plan, digest = self.plan()
        (self.repo/'new.txt').write_text('new source')
        result = self.cli('run','--plan',plan,'--plan-sha256',digest,'--out',self.root/'bundle')
        self.assertNotEqual(result.returncode, 0)

    def test_candidate_mutated_during_check_is_not_passed(self):
        _, _, receipt = self.run_check('mutate')
        self.assertNotEqual(receipt['verdict'], 'passed')

    def test_report_fetches_failed_verdict_but_gate_refuses(self):
        _, bundle, _ = self.run_check('wrong')
        self.assertEqual(self.cli('report','--bundle',bundle).returncode,0)
        self.assertNotEqual(self.cli('gate','--bundle',bundle).returncode,0)

    def test_forged_expected_observation_is_rejected(self):
        _, bundle, receipt = self.run_check('wrong')
        receipt['checks'][0]['expected']={'answer':41}
        receipt['checks'][0]['status']='passed'
        receipt['verdict']='passed'
        (bundle/'receipt.json').write_text(json.dumps(receipt))
        self.assertNotEqual(self.cli('report','--bundle',bundle).returncode,0)

    def test_log_tampering_is_rejected_by_report(self):
        _, bundle, receipt = self.run_check()
        paths = [bundle/a['path'] for a in receipt['artifact_manifest'] if 'stdout' in a['path']]
        self.assertTrue(paths)
        paths[0].write_text('tampered')
        result = self.cli('report','--bundle',bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(json.loads(result.stdout).get('verdict'), 'passed')

    def test_plan_tampering_is_rejected_by_report(self):
        _, bundle, _ = self.run_check()
        (bundle/'plan.json').write_text('{}\n')
        self.assertNotEqual(self.cli('report','--bundle',bundle).returncode, 0)

    def test_status_tampering_is_rejected_by_report(self):
        _, bundle, receipt = self.run_check('wrong')
        receipt['checks'][0]['status']='passed'
        receipt['verdict']='passed'
        (bundle/'receipt.json').write_text(json.dumps(receipt))
        self.assertNotEqual(self.cli('report','--bundle',bundle).returncode, 0)

    def test_bundle_reuse_never_redispatches(self):
        _, bundle, _ = self.run_check()
        before=(bundle/'receipt.json').read_bytes()
        plan=self.root/'plan.json'
        result=self.cli('run','--plan',plan,'--plan-sha256',hashlib.sha256(plan.read_bytes()).hexdigest(),'--out',bundle)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(before,(bundle/'receipt.json').read_bytes())

    def test_sensitive_log_is_redacted(self):
        _, bundle, receipt = self.run_check('secret')
        for artifact in receipt['artifact_manifest']:
            self.assertNotIn(b'secret-example-value',(bundle/artifact['path']).read_bytes())

    def test_unsupported_schema_refuses_before_execution(self):
        plan, digest = self.plan()
        value=json.loads(plan.read_text())
        value['version']=999
        plan.write_text(json.dumps(value))
        digest=hashlib.sha256(plan.read_bytes()).hexdigest()
        result=self.cli('run','--plan',plan,'--plan-sha256',digest,'--out',self.root/'bundle')
        self.assertNotEqual(result.returncode,0)

    def test_fixture_path_escape_refuses_plan(self):
        spec={'version':1,'workflow_id':'bootstrap','repository':str(self.repo),'candidate_commit':self.head,'checks':[{'id':'C1','required':True,'argv':[PYTHON,str(self.repo/'check.py'),'passed'],'expected':{'answer':42},'fixtures':['../outside'],'timeout_seconds':1}]}
        (self.root/'outside').write_text('outside')
        path=self.root/'unsafe.json'
        path.write_text(json.dumps(spec))
        result=self.cli('plan','--spec',path,'--out',self.root/'unsafe-plan.json')
        self.assertNotEqual(result.returncode,0)

    def test_incomplete_bundle_is_blocked_without_execution(self):
        bundle=self.root/'unfinished'
        bundle.mkdir()
        result=self.cli('report','--bundle',bundle)
        self.assertNotEqual(result.returncode,0)


if __name__ == '__main__':
    unittest.main()
