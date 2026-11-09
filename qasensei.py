#!/usr/bin/env python3
"""Independent local QA runner. Trusted checks only; no provider or live-state calls."""
import importlib.metadata
import argparse, hashlib, json, math, os, platform, re, selectors, shutil, signal, stat, subprocess, sys, tempfile, time
from datetime import datetime, timezone
CAP = 1048576
STATUSES = ('passed', 'failed', 'blocked', 'skipped', 'flaky', 'not_run')
S40 = re.compile('^[0-9a-f]{40}$')
S64 = re.compile('^[0-9a-f]{64}$')

class E(Exception):
    pass

def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def canon(o):
    return (json.dumps(o, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')

def emit(o):
    sys.stdout.buffer.write(canon(o))

def sha_b(b):
    return hashlib.sha256(b).hexdigest()

def no_dup(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError('duplicate JSON field')
        d[k] = v
    return d

def loads(b):
    try:
        if isinstance(b, bytes):
            b = b.decode('utf-8')
        return json.loads(b, object_pairs_hook=no_dup, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except Exception as e:
        raise E('invalid JSON: ' + str(e))

def closed(o, keys, what):
    if not isinstance(o, dict) or set(o) != set(keys):
        raise E('invalid ' + what)

def is_int(x):
    return type(x) is int

def fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass

def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError('short write')
        view = view[written:]

def write_atomic(path, data):
    d = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-')
    try:
        os.fchmod(fd, 384)
        write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        fsync_dir(d)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass

def write_exclusive(path, data):
    if os.path.lexists(path):
        raise E('output already exists')
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-')
    try:
        os.fchmod(fd, 384)
        write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.link(tmp, path)
        fsync_dir(d)
    except FileExistsError:
        raise E('output already exists')
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass

def fp(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise E('not a regular file: ' + str(path))
    h = hashlib.sha256()
    n = 0
    with open(path, 'rb') as f:
        while True:
            c = f.read(65536)
            if not c:
                break
            h.update(c)
            n += len(c)
    return (h.hexdigest(), n)

def read_file(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise E('not a regular file: ' + str(path))
    with open(path, 'rb') as f:
        data = f.read(CAP + 1)
    if len(data) > CAP:
        raise E('input exceeds 1 MiB')
    return data

def git(repo, args):
    g = shutil.which('git') or '/usr/bin/git'
    env = {k: os.environ[k] for k in ('HOME', 'PATH') if k in os.environ}
    env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C'})
    a = [g, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'protocol.ext.allow=never', '-C', repo] + args
    try:
        r = subprocess.run(a, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=env)
    except Exception as e:
        raise E('git failed: ' + str(e))
    if r.returncode != 0:
        raise E('git failed: ' + r.stderr.decode('utf-8', 'replace').strip()[:200])
    return r.stdout

def candidate(repo_given, commit):
    repo = os.path.realpath(repo_given)
    if not os.path.isdir(repo):
        raise E('repository is not a directory')
    top = git(repo, ['rev-parse', '--show-toplevel']).decode('utf-8').strip()
    if os.path.realpath(top) != repo:
        raise E('repository is not a Git top-level')
    head = git(repo, ['rev-parse', 'HEAD']).decode('utf-8').strip()
    if head != commit:
        raise E('candidate commit mismatch')
    tree = git(repo, ['rev-parse', commit + '^{tree}']).decode('utf-8').strip()
    if git(repo, ['ls-files', '--others', '--exclude-standard', '-z']):
        raise E('candidate has untracked files')
    entries = git(repo, ['ls-files', '--stage', '-z']).split(b'\x00')
    tree_entries = git(repo, ['ls-tree', '-r', '-z', 'HEAD']).split(b'\x00')
    tree_map = {}
    for entry in tree_entries:
        if entry:
            meta, path = entry.split(b'\t', 1)
            mode, kind, oid = meta.split()
            tree_map[path] = (mode, oid)
    man = []
    seen = set()
    for entry in entries:
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        mode, oid, stage = meta.split()
        if stage != b'0' or tree_map.get(path) != (mode, oid):
            raise E('candidate index differs from HEAD')
        if mode not in (b'100644', b'100755'):
            raise E('symlink or gitlink in candidate')
        rel = os.fsdecode(path)
        full = os.path.join(repo, rel)
        digest, size = fp(full)
        raw_hash = hashlib.sha1(b'blob ' + str(size).encode('ascii') + b'\0')
        with open(full, 'rb') as stream:
            while chunk := stream.read(65536):
                raw_hash.update(chunk)
        if raw_hash.hexdigest().encode('ascii') != oid:
            raise E('candidate bytes differ from HEAD')
        if bool(os.stat(full).st_mode & 0o111) != (mode == b'100755'):
            raise E('candidate file mode differs from HEAD')
        man.append({'path': rel, 'sha256': digest, 'size': size})
        seen.add(path)
    if seen != set(tree_map):
        raise E('candidate tracked files differ from HEAD')
    man.sort(key=lambda x: x['path'])
    return {'repository': repo, 'commit': head, 'tree': tree, 'manifest': man, 'manifest_sha256': sha_b(canon(man))}

def v_spec(s):
    closed(s, {'version', 'workflow_id', 'repository', 'candidate_commit', 'checks'}, 'spec')
    if not is_int(s['version']) or s['version'] != 1:
        raise E('unsupported spec version')
    if not isinstance(s['workflow_id'], str) or not s['workflow_id']:
        raise E('invalid workflow_id')
    if not isinstance(s['repository'], str) or not os.path.isabs(s['repository']):
        raise E('repository must be absolute')
    if not isinstance(s['candidate_commit'], str) or not S40.match(s['candidate_commit']):
        raise E('invalid candidate_commit')
    if not isinstance(s['checks'], list) or not 1 <= len(s['checks']) <= 32:
        raise E('invalid checks')
    ids = set()
    for c in s['checks']:
        closed(c, {'id', 'required', 'argv', 'expected', 'fixtures', 'timeout_seconds'}, 'check')
        if not isinstance(c['id'], str) or not c['id'] or c['id'] in ids:
            raise E('invalid or duplicate check id')
        ids.add(c['id'])
        if type(c['required']) is not bool:
            raise E('invalid required')
        if not isinstance(c['argv'], list) or not c['argv'] or any((not isinstance(x, str) for x in c['argv'])) or (not os.path.isabs(c['argv'][0])):
            raise E('invalid argv')
        if not isinstance(c['expected'], dict) or len(canon(c['expected'])) > 16384:
            raise E('expected must be an object')
        if not isinstance(c['fixtures'], list):
            raise E('invalid fixtures')
        for fx in c['fixtures']:
            parts = fx.split('/') if isinstance(fx, str) else []
            if not isinstance(fx, str) or not fx or os.path.isabs(fx) or any((p in ('', '..') for p in parts)):
                raise E('invalid fixture path')
        t = c['timeout_seconds']
        if type(t) not in (int, float) or not math.isfinite(t) or (not 1 <= t <= 60):
            raise E('invalid timeout_seconds')
    return s

def check_fps(spec):
    repo = os.path.realpath(spec['repository'])
    out = []
    for c in spec['checks']:
        ex = c['argv'][0]
        res = os.path.realpath(ex)
        st = os.lstat(res)
        if not stat.S_ISREG(st.st_mode) or not os.access(res, os.X_OK):
            raise E('executable is not a regular executable file')
        if res != os.path.realpath(sys.executable) or len(c['argv']) < 2:
            raise E('v1 requires the current Python interpreter and repository script')
        script = c['argv'][1]
        if not os.path.isabs(script):
            script = os.path.join(repo, script)
        script = os.path.realpath(script)
        if os.path.commonpath([repo, script]) != repo or not script.endswith('.py'):
            raise E('v1 checker must be a repository Python script')
        rel_script = os.path.relpath(script, repo).replace(os.sep, '/')
        if rel_script not in c['fixtures']:
            raise E('checker script must be a declared fixture')
        d, sz = fp(res)
        fxs = []
        for rel in c['fixtures']:
            full = os.path.join(repo, rel)
            if os.path.islink(full):
                raise E('fixture is a symlink')
            rr = os.path.realpath(full)
            if os.path.commonpath([repo, rr]) != repo:
                raise E('fixture outside repository')
            fd, fs = fp(rr)
            fxs.append({'path': rel, 'sha256': fd, 'size': fs})
        out.append({'id': c['id'], 'executable': {'path': ex, 'resolved_path': res, 'sha256': d, 'size': sz}, 'fixtures': fxs})
    return out

def environment():
    executable = os.path.realpath(sys.executable)
    git_path = os.path.realpath(shutil.which('git') or '/usr/bin/git')
    packages = sorted({(d.metadata.get('Name', 'unknown'), d.version) for d in importlib.metadata.distributions()})
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'executable': executable, 'executable_sha256': fp(executable)[0],
            'git': git_path, 'git_sha256': fp(git_path)[0],
            'dependencies': [list(p) for p in packages]}

def build_plan(spec):
    v_spec(spec)
    cand = candidate(spec['repository'], spec['candidate_commit'])
    fps = check_fps(spec)
    return {'version': 1, 'kind': 'qa_plan', 'created_at': now(), 'spec': spec, 'candidate': cand, 'checks': fps, 'environment': environment()}

def v_plan(p):
    closed(p, {'version', 'kind', 'created_at', 'spec', 'candidate', 'checks', 'environment'}, 'plan')
    if not is_int(p['version']) or p['version'] != 1 or p['kind'] != 'qa_plan' or (not isinstance(p['created_at'], str)):
        raise E('invalid plan')
    closed(p['environment'], {'python','platform','executable','executable_sha256','git','git_sha256','dependencies'}, 'environment')
    v_spec(p['spec'])
    cand = p['candidate']
    closed(cand, {'repository', 'commit', 'tree', 'manifest', 'manifest_sha256'}, 'candidate')
    if not os.path.isabs(cand.get('repository', '')) or cand['commit'] != p['spec']['candidate_commit'] or (not S40.match(cand.get('tree', ''))) or (not S64.match(cand.get('manifest_sha256', ''))):
        raise E('invalid candidate')
    if not isinstance(cand['manifest'], list):
        raise E('invalid manifest')
    seen = set()
    for m in cand['manifest']:
        closed(m, {'path', 'sha256', 'size'}, 'manifest entry')
        if not isinstance(m['path'], str) or m['path'] in seen or (not S64.match(m.get('sha256', ''))) or (not is_int(m['size'])) or (m['size'] < 0):
            raise E('invalid manifest entry')
        seen.add(m['path'])
    if [m['path'] for m in cand['manifest']] != sorted(seen) or sha_b(canon(cand['manifest'])) != cand['manifest_sha256']:
        raise E('invalid manifest digest')
    if not isinstance(p['checks'], list) or len(p['checks']) != len(p['spec']['checks']):
        raise E('invalid plan checks')
    for pc, sc in zip(p['checks'], p['spec']['checks']):
        closed(pc, {'id', 'executable', 'fixtures'}, 'plan check')
        closed(pc['executable'], {'path', 'resolved_path', 'sha256', 'size'}, 'executable')
        if pc['id'] != sc['id'] or pc['executable']['path'] != sc['argv'][0] or (not S64.match(pc['executable'].get('sha256', ''))) or (not is_int(pc['executable']['size'])):
            raise E('invalid plan check')
        if not isinstance(pc['fixtures'], list) or len(pc['fixtures']) != len(sc['fixtures']):
            raise E('invalid plan fixtures')
        for pf, sf in zip(pc['fixtures'], sc['fixtures']):
            closed(pf, {'path', 'sha256', 'size'}, 'plan fixture')
            if pf['path'] != sf or not S64.match(pf.get('sha256', '')) or (not is_int(pf['size'])):
                raise E('invalid plan fixture')
    return p

def sanitize(b):
    b = re.sub(rb'(?i)(authorization[\"\']?\s*[:=]\s*[\"\']?(?:bearer|basic)?\s*)[^\s,;}\"\']+', rb'\1[REDACTED]', b)
    b = re.sub(rb'(?i)(\b(?:api[-_ ]?key|access[-_ ]?token|token|password|passwd|secret)[\"\']?\s*[:=]\s*[\"\']?)[^\s,;}\"\']+', rb'\1[REDACTED]', b)
    b = re.sub(rb'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+', rb'\1[REDACTED]', b)
    return b

def parse_proto(b):
    try:
        t = b.decode('utf-8').strip()
        if not t:
            return None
        dec = json.JSONDecoder(object_pairs_hook=no_dup, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        o, i = dec.raw_decode(t)
        if t[i:].strip():
            return None
        if not isinstance(o, dict) or set(o) != {'version', 'status', 'observed', 'effects'}:
            return None
        if not is_int(o['version']) or o['version'] != 1 or o['status'] not in STATUSES or (not isinstance(o['observed'], dict)) or (not isinstance(o['effects'], list)):
            return None
        if len(canon([o['observed'], o['effects']])) > 16384:
            return None
        return o
    except Exception:
        return None

def evidence(bundle, kind, data):
    d = sha_b(data)
    rel = 'evidence/' + d + '.' + kind + '.log'
    full = os.path.join(bundle, rel)
    if not os.path.exists(full):
        write_atomic(full, data)
    return {'path': rel, 'sha256': d, 'size': len(data)}

def base_receipt(c, repo):
    return {'check_spec_sha256': sha_b(canon(c)), 'id': c['id'], 'required': c['required'], 'argv': c['argv'], 'cwd': repo, 'started_at': now(), 'ended_at': now(), 'elapsed_seconds': 0.0, 'exit_code': None, 'timeout': False, 'output_capped': False, 'expected': c['expected'], 'observed': {}, 'effects': [], 'reported_status': None, 'status': 'not_run', 'reason': None, 'stdout': None, 'stderr': None}

def placeholder(bundle, c, repo, reason):
    r = base_receipt(c, repo)
    r['reason'] = reason
    r['stdout'] = evidence(bundle, 'stdout', b'')
    r['stderr'] = evidence(bundle, 'stderr', b'')
    return r

def kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass

def exec_check(bundle, c, repo):
    r = base_receipt(c, repo)
    r['started_at'] = now()
    start = time.monotonic()
    out = bytearray()
    err = bytearray()
    timed = False
    capped = False
    spawn = None
    env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'LC_ALL') if k in os.environ}
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        p = subprocess.Popen(c['argv'], cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except Exception as e:
        spawn = str(e)
        p = None
        r['status'] = 'blocked'
        r['reason'] = 'spawn_error: ' + spawn
    if p is not None:
        try:
            sel = selectors.DefaultSelector()
            bufs = {p.stdout.fileno(): out, p.stderr.fileno(): err}
            for f in (p.stdout, p.stderr):
                os.set_blocking(f.fileno(), False)
                sel.register(f, selectors.EVENT_READ)
            deadline = start + float(c['timeout_seconds'])
            while sel.get_map():
                rem = deadline - time.monotonic()
                if rem <= 0:
                    timed = True
                    break
                ev = sel.select(min(rem, 0.05))
                stop = False
                for key, _ in ev:
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    bufs[key.fd].extend(chunk)
                    if len(out) + len(err) > CAP:
                        capped = True
                        stop = True
                        break
                if stop:
                    break
            sel.close()
            if timed or capped:
                kill_group(p)
                p.wait()
            else:
                try:
                    p.wait(timeout=max(0.0, deadline - time.monotonic()) + 0.1)
                except subprocess.TimeoutExpired:
                    timed = True
                    kill_group(p)
                    p.wait()
        finally:
            kill_group(p)
            p.wait(timeout=2)
            p.stdout.close()
            p.stderr.close()
        r['exit_code'] = p.returncode
    raw_out = bytes(out[:CAP])
    raw_err = bytes(err[:max(0, CAP - len(raw_out))])
    sanitized_out = sanitize(raw_out)[:CAP]
    stdout_redacted = sanitized_out != raw_out
    proto = parse_proto(sanitized_out) if p is not None and not stdout_redacted else None
    if proto is not None:
        r['observed'] = proto['observed']
        r['effects'] = proto['effects']
        r['reported_status'] = proto['status']
    r['timeout'] = timed
    r['output_capped'] = capped
    if spawn is None:
        if timed or capped:
            r['status'] = 'blocked'
            r['reason'] = 'timeout' if timed else 'output_cap'
        elif r['exit_code'] != 0:
            r['status'] = 'failed'
            r['reason'] = 'nonzero_exit'
        elif proto is None:
            r['status'] = 'blocked'
            r['reason'] = 'stdout_redacted' if stdout_redacted else 'malformed_stdout'
        elif proto['status'] == 'passed':
            r['status'] = 'passed' if canon(proto['observed']) == canon(c['expected']) else 'failed'
            r['reason'] = None if r['status'] == 'passed' else 'expected_mismatch'
        else:
            r['status'] = proto['status']
            r['reason'] = 'reported_' + proto['status']
    r['stdout'] = evidence(bundle, 'stdout', sanitized_out)
    r['stderr'] = evidence(bundle, 'stderr', sanitize(raw_err)[:max(0,CAP-len(sanitized_out))])
    r['ended_at'] = now()
    r['elapsed_seconds'] = round(time.monotonic() - start, 6)
    return r

def counts_of(checks):
    return {s: sum((1 for c in checks if c['status'] == s)) for s in STATUSES}

def derive(checks, unchanged):
    if any((c['status'] == 'failed' for c in checks)):
        return 'failed'
    if any((c['status'] == 'blocked' for c in checks)):
        return 'incomplete'
    if any((c['required'] and c['status'] != 'passed' for c in checks)):
        return 'incomplete'
    if not unchanged:
        return 'incomplete'
    return 'passed'

def art_entry(bundle, rel):
    if not safe_rel(rel):
        raise E('unsafe artifact location')
    root = os.path.realpath(bundle)
    current = root
    for part in rel.split('/'):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise E('symlink artifact location')
    if os.path.commonpath([root, os.path.realpath(current)]) != root:
        raise E('artifact escapes bundle')
    d, s = fp(current)
    return {'path': rel, 'size': s, 'sha256': d}

def finalize(bundle, plan, plan_sha, checks, unchanged, started, err):
    paths = sorted({'plan.json'} | {c[k]['path'] for c in checks for k in ('stdout', 'stderr')})
    runner, _ = fp(os.path.realpath(__file__))
    receipt = {'version': 1, 'kind': 'qa_report', 'producer': 'qasensei', 'workflow_id': plan['spec']['workflow_id'], 'started_at': started, 'ended_at': now(), 'candidate': plan['candidate'], 'candidate_unchanged': bool(unchanged), 'plan_sha256': plan_sha, 'runner_sha256': runner, 'environment': plan['environment'], 'checks': checks, 'artifact_manifest': [art_entry(bundle, p) for p in paths], 'counts': counts_of(checks), 'verdict': derive(checks, unchanged), 'error': err}
    write_atomic(os.path.join(bundle, 'receipt.json'), canon(receipt))
    re_entry = art_entry(bundle, 'receipt.json')
    man = {'version': 1, 'kind': 'qa_bundle_manifest', 'plan_sha256': plan_sha, 'receipt_sha256': re_entry['sha256'], 'artifacts': sorted(receipt['artifact_manifest'] + [re_entry], key=lambda x: x['path'])}
    write_atomic(os.path.join(bundle, 'manifest.json'), canon(man))
    write_atomic(os.path.join(bundle, 'COMPLETE'), canon({'version': 1, 'kind': 'qa_run_marker', 'state': 'complete', 'plan_sha256': plan_sha}))
    try:
        os.unlink(os.path.join(bundle, 'IN_PROGRESS'))
    except OSError:
        pass
    fsync_dir(bundle)
    return receipt

def summary(receipt, bundle):
    return {'version': 1, 'kind': 'qa_report', 'status': receipt['verdict'], 'verdict': receipt['verdict'], 'workflow_id': receipt['workflow_id'], 'candidate_commit': receipt['candidate']['commit'], 'plan_sha256': receipt['plan_sha256'], 'bundle': bundle, 'counts': receipt['counts'], 'checks': [{'id': c['id'], 'status': c['status'], 'required': c['required']} for c in receipt['checks']], 'error': receipt['error']}

def code_of(v):
    return 0 if v == 'passed' else 1 if v == 'failed' else 2

def cmd_plan(a):
    spec = loads(read_file(a.spec))
    plan = build_plan(spec)
    data = canon(plan)
    if len(data) > CAP // 4:
        raise E('plan exceeds 256 KiB')
    parent = os.path.dirname(os.path.abspath(a.out))
    if not os.path.isdir(parent):
        raise E('output parent does not exist')
    write_exclusive(a.out, data)
    emit({'version': 1, 'kind': 'qa_plan_result', 'status': 'passed', 'verdict': 'passed', 'plan_sha256': sha_b(data), 'plan': os.path.abspath(a.out), 'workflow_id': spec['workflow_id'], 'candidate_commit': spec['candidate_commit']})
    return 0

def cmd_run(a):
    if not S64.match(a.plan_sha256 or ''):
        raise E('invalid plan_sha256')
    plan_bytes = read_file(a.plan)
    if sha_b(plan_bytes) != a.plan_sha256:
        raise E('plan_sha256 mismatch')
    plan = v_plan(loads(plan_bytes))
    spec = plan['spec']
    repo = plan['candidate']['repository']
    if os.path.lexists(a.out):
        raise E('bundle already exists')
    bundle = os.path.realpath(a.out)
    if not os.path.isdir(os.path.dirname(bundle)):
        raise E('bundle parent does not exist')
    if os.path.commonpath([repo, bundle]) == repo:
        raise E('bundle must be outside candidate')
    os.mkdir(bundle, 448)
    started = now()
    checks = []
    unchanged = True
    err = None
    try:
        os.mkdir(os.path.join(bundle, 'evidence'), 448)
        write_atomic(os.path.join(bundle, 'plan.json'), plan_bytes)
        write_atomic(os.path.join(bundle, 'IN_PROGRESS'), canon({'version': 1, 'kind': 'qa_run_marker', 'state': 'running', 'started_at': started, 'plan_sha256': a.plan_sha256}))
        try:
            cand2 = candidate(spec['repository'], spec['candidate_commit'])
            fps2 = check_fps(spec)
            if cand2 != plan['candidate'] or fps2 != plan['checks'] or environment() != plan['environment']:
                raise E('stale candidate or checker identity')
        except E as e:
            err = str(e)
            unchanged = False
            checks = [placeholder(bundle, c, repo, 'precheck_blocked') for c in spec['checks']]
        else:
            for c in spec['checks']:
                checks.append(exec_check(bundle, c, repo))
            try:
                unchanged = candidate(spec['repository'], spec['candidate_commit']) == plan['candidate']
            except Exception as e:
                unchanged = False
                err = 'candidate changed after checks: ' + str(e)
    except BaseException as e:
        err = 'interrupted: ' + str(e)
        unchanged = False
        done = {c['id'] for c in checks}
        for c in spec['checks']:
            if c['id'] not in done:
                checks.append(placeholder(bundle, c, repo, 'interrupted'))
    receipt = finalize(bundle, plan, a.plan_sha256, checks, unchanged, started, err)
    emit(summary(receipt, bundle))
    if err and not any(c['exit_code'] is not None for c in checks):
        return 3
    return 0

def safe_rel(p):
    return isinstance(p, str) and p and (not os.path.isabs(p)) and all((x not in ('', '.', '..') for x in p.split('/')))

def verify_bundle(bundle):
    bundle = os.path.realpath(bundle)
    if not os.path.isdir(bundle) or os.path.lexists(os.path.join(bundle, 'IN_PROGRESS')) or (not os.path.exists(os.path.join(bundle, 'COMPLETE'))):
        raise E('unfinished bundle')
    for name in ('plan.json', 'receipt.json', 'manifest.json'):
        if not os.path.isfile(os.path.join(bundle, name)):
            raise E('unfinished bundle')
    man = loads(read_file(os.path.join(bundle, 'manifest.json')))
    closed(man, {'version', 'kind', 'plan_sha256', 'receipt_sha256', 'artifacts'}, 'bundle manifest')
    if man['version'] != 1 or man['kind'] != 'qa_bundle_manifest' or (not isinstance(man['artifacts'], list)):
        raise E('invalid bundle manifest')
    arts = {}
    for e in man['artifacts']:
        closed(e, {'path', 'size', 'sha256'}, 'artifact')
        if not safe_rel(e['path']) or e['path'] in arts or (not is_int(e['size'])) or (not S64.match(e.get('sha256', ''))):
            raise E('invalid artifact')
        actual = art_entry(bundle, e['path'])
        if actual != e:
            raise E('artifact hash mismatch: ' + e['path'])
        arts[e['path']] = e
    plan_bytes = read_file(os.path.join(bundle, 'plan.json'))
    plan_sha = sha_b(plan_bytes)
    plan = v_plan(loads(plan_bytes))
    receipt_bytes = read_file(os.path.join(bundle, 'receipt.json'))
    receipt = loads(receipt_bytes)
    if plan_sha != man['plan_sha256'] or sha_b(receipt_bytes) != man['receipt_sha256']:
        raise E('plan or receipt digest mismatch')
    closed(receipt, {'version', 'kind', 'producer', 'workflow_id', 'started_at', 'ended_at', 'candidate', 'candidate_unchanged', 'plan_sha256', 'runner_sha256', 'environment', 'checks', 'artifact_manifest', 'counts', 'verdict', 'error'}, 'receipt')
    if receipt['version'] != 1 or receipt['kind'] != 'qa_report' or receipt['producer'] != 'qasensei' or (receipt['workflow_id'] != plan['spec']['workflow_id']) or (receipt['candidate'] != plan['candidate']) or (receipt['plan_sha256'] != plan_sha) or (not S64.match(receipt.get('runner_sha256', ''))) or (type(receipt['candidate_unchanged']) is not bool) or (receipt['verdict'] not in ('passed', 'failed', 'incomplete')):
        raise E('invalid receipt')
    if receipt['environment'] != plan['environment']:
        raise E('environment identity mismatch')
    expect_arts = sorted((v for k, v in arts.items() if k != 'receipt.json'), key=lambda x: x['path'])
    if receipt['artifact_manifest'] != expect_arts:
        raise E('receipt artifact manifest mismatch')
    specs = plan['spec']['checks']
    checks = receipt['checks']
    if not isinstance(checks, list) or len(checks) != len(specs):
        raise E('check count mismatch')
    ev_paths = set()
    for rc, sc in zip(checks, specs):
        closed(rc, {'check_spec_sha256', 'id', 'required', 'argv', 'cwd', 'started_at', 'ended_at', 'elapsed_seconds', 'exit_code', 'timeout', 'output_capped', 'expected', 'observed', 'effects', 'reported_status', 'status', 'reason', 'stdout', 'stderr'}, 'check receipt')
        if rc['check_spec_sha256'] != sha_b(canon(sc)) or rc['id'] != sc['id'] or rc['required'] != sc['required'] or rc['argv'] != sc['argv'] or (rc['expected'] != sc['expected']) or (rc['cwd'] != plan['candidate']['repository']) or (rc['status'] not in STATUSES):
            raise E('check receipt mismatch')
        for k in ('stdout', 'stderr'):
            closed(rc[k], {'path', 'size', 'sha256'}, 'evidence')
            ev_paths.add(rc[k]['path'])
            if arts.get(rc[k]['path']) != rc[k]:
                raise E('evidence manifest mismatch')
        if rc['exit_code'] is None and rc['status'] not in ('not_run', 'blocked'):
            raise E('unexecuted check cannot pass')
        if rc['exit_code'] is not None:
            data = read_file(os.path.join(bundle, rc['stdout']['path']))
            proto = parse_proto(data)
            if rc['timeout'] or rc['output_capped'] or rc['reason'] == 'stdout_redacted':
                derived = 'blocked'
            elif rc['exit_code'] != 0:
                derived = 'failed'
            elif proto is None:
                derived = 'blocked'
            elif proto['status'] == 'passed':
                derived = 'passed' if canon(proto['observed']) == canon(rc['expected']) else 'failed'
            else:
                derived = proto['status']
            if derived != rc['status']:
                raise E('check status mismatch')
            if proto is not None and rc['reason'] != 'stdout_redacted' and (canon(proto['observed']) != canon(rc['observed']) or canon(proto['effects']) != canon(rc['effects']) or proto['status'] != rc['reported_status']):
                raise E('check observed mismatch')
    if set(arts) != {'plan.json', 'receipt.json'} | ev_paths:
        raise E('artifact set mismatch')
    if receipt['counts'] != counts_of(checks) or receipt['verdict'] != derive(checks, receipt['candidate_unchanged']):
        raise E('counts or verdict mismatch')
    return receipt, plan, plan_sha

def artifact_ref(bundle, name, receipt):
    path = os.path.join(os.path.realpath(bundle), name)
    digest, size = fp(path)
    return {'path': path, 'sha256': digest, 'bytes': size,
            'media_type': 'application/json', 'producer': 'qasensei',
            'created_at': receipt['ended_at']}

def normalized_sha(value):
    # Shared contracts hash compact finite sorted UTF-8 JSON without trailing LF.
    return sha_b(canon(value)[:-1])

def common_candidate(plan, base_commit):
    if not S40.fullmatch(base_commit):
        raise E('invalid base commit')
    candidate_now = candidate(plan['candidate']['repository'], plan['candidate']['commit'])
    if candidate_now != plan['candidate']:
        raise E('candidate changed')
    repo = candidate_now['repository']
    diff = git(repo, ['diff', '--no-ext-diff', '--no-textconv', '--binary', base_commit, candidate_now['commit'], '--'])
    fixtures = {f['path']: f['sha256'] for check in plan['checks'] for f in check['fixtures']}
    return {'schema': 'sensei.candidate/1.0', 'repository': repo,
            'base_commit': base_commit, 'commit': candidate_now['commit'],
            'tree': candidate_now['tree'], 'diff_sha256': sha_b(diff),
            'dirty': False, 'untracked_sha256': normalized_sha([]),
            'fixture_sha256': normalized_sha(fixtures),
            'environment_sha256': normalized_sha(plan['environment'])}

def cmd_candidate(a):
    plan = v_plan(loads(read_file(a.plan)))
    value = common_candidate(plan, a.base_commit)
    write_exclusive(a.out, canon(value))
    emit({'schema': 'sensei.candidate/1.0', 'candidate': value,
          'path': os.path.abspath(a.out), 'sha256': sha_b(canon(value))})
    return 0

def cmd_report(a):
    receipt, plan, plan_sha = verify_bundle(a.bundle)
    value = summary(receipt, os.path.realpath(a.bundle))
    value.update({'execution_status': 'completed', 'evidence_integrity': 'passed',
                  'authenticity': 'not_established', 'candidate': receipt['candidate'],
                  'environment_sha256': normalized_sha(receipt['environment']),
                  'receipt': artifact_ref(a.bundle, 'receipt.json', receipt),
                  'manifest': artifact_ref(a.bundle, 'manifest.json', receipt)})
    emit(value)
    return code_of(receipt['verdict']) if a.cmd == 'gate' else 0

def cmd_export(a):
    receipt, plan, plan_sha = verify_bundle(a.bundle)
    shared = loads(read_file(a.candidate))
    if not isinstance(shared, dict) or shared != common_candidate(plan, shared.get('base_commit', '')):
        raise E('shared candidate does not match the QA source/fixture/environment')
    native = artifact_ref(a.bundle, 'receipt.json', receipt)
    manifest = artifact_ref(a.bundle, 'manifest.json', receipt)
    checks = [{'id': c['id'], 'execution': 'completed' if c['exit_code'] is not None else 'not_run',
               'outcome': c['status'], 'expected': c['expected'], 'observed': c['observed'],
               'check_spec_sha256': c['check_spec_sha256'], 'evidence_refs': [native['sha256']],
               'effects': c['effects']} for c in receipt['checks']]
    emit({'schema': 'sensei.qa/1.0', 'task_id': a.task_id, 'suite': a.suite,
          'workflow_id': receipt['workflow_id'], 'candidate_commit': shared['commit'],
          'candidate': shared, 'environment_ref': normalized_sha(receipt['environment']),
          'checks': checks, 'coverage': {'required': sum(c['required'] for c in receipt['checks']),
          'executed': sum(c['exit_code'] is not None for c in receipt['checks']),
          'not_run': sum(c['exit_code'] is None for c in receipt['checks'])},
          'verdict': receipt['verdict'], 'execution_status': 'completed',
          'plan_sha256': plan_sha, 'native_receipt': native, 'manifest': manifest})
    return 0

def cmd_capabilities(a):
    emit({'schema': 'sensei.qa.capabilities/1.0', 'operations': ['plan','candidate','run','report','gate','export'],
          'runner_profile': 'python-json-check/1', 'schemas': ['sensei.qa/1.0','sensei.candidate/1.0'],
          'canonical_outcomes': list(STATUSES), 'verdicts': ['passed','failed','incomplete'],
          'limits': {'checks':32,'timeout_seconds_per_check':60,'combined_output_bytes_per_check':CAP,
          'input_bytes':CAP,'plan_bytes':CAP//4,'observation_bytes_per_check':16384},
          'source': 'clean SHA-1 Git commits only; dirty/untracked/symlink/gitlink refused',
          'resume': 'report existing evidence only; incomplete bundles never redispatch',
          'containment': 'trusted local process group; not an OS sandbox',
          'legacy_pass': 'not emitted; this is the first implementation'})
    return 0

class AP(argparse.ArgumentParser):

    def error(self, msg):
        raise E(msg)

def main(argv=None):
    p = AP(prog='qasensei')
    sub = p.add_subparsers(dest='cmd', required=True)
    x = sub.add_parser('plan')
    x.add_argument('--spec', required=True)
    x.add_argument('--out', required=True)
    x = sub.add_parser('run')
    x.add_argument('--plan', required=True)
    x.add_argument('--plan-sha256', required=True)
    x.add_argument('--out', required=True)
    for name in ('report', 'gate'):
        x = sub.add_parser(name)
        x.add_argument('--bundle', required=True)
    x = sub.add_parser('candidate')
    x.add_argument('--plan', required=True)
    x.add_argument('--base-commit', required=True)
    x.add_argument('--out', required=True)
    x = sub.add_parser('export')
    x.add_argument('--bundle', required=True)
    x.add_argument('--candidate', required=True)
    x.add_argument('--task-id', required=True)
    x.add_argument('--suite', required=True)
    x = sub.add_parser('capabilities')
    x.add_argument('--json', action='store_true')
    try:
        a = p.parse_args(argv)
        return {'plan': cmd_plan, 'run': cmd_run, 'report': cmd_report, 'gate': cmd_report, 'candidate':cmd_candidate, 'export':cmd_export, 'capabilities':cmd_capabilities}[a.cmd](a)
    except E as e:
        emit({'version': 1, 'status': 'blocked', 'verdict': 'blocked', 'error': str(e)})
        return 2
    except BrokenPipeError:
        return 2
    except Exception as e:
        emit({'version': 1, 'status': 'blocked', 'verdict': 'blocked', 'error': str(e)})
        return 2
if __name__ == '__main__':
    raise SystemExit(main())
