"""Fetch only the three published dictionary views through an encrypted SSH tunnel."""
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

VIEWS = ('current_published_fields', 'current_published_field_aliases',
         'current_published_value_contracts')
DEFAULTS = Path(__file__).resolve().parents[1] / '服务器连接.json'


def connection_config():
    cfg = json.loads(DEFAULTS.read_text(encoding='utf-8'))
    supplied = os.environ.get('XIEHE_DICTIONARY_CONFIG')
    if supplied:
        path = Path(supplied).expanduser().resolve()
        skill = DEFAULTS.parents[3]
        if path.is_relative_to(skill):
            raise ValueError('连接凭据必须放在 Skill 目录之外')
        if path.stat().st_mode & 0o077:
            raise ValueError('连接配置含敏感信息，文件权限须为 0600')
        cfg.update(json.loads(path.read_text(encoding='utf-8')))
    for key in ('ssh_host', 'ssh_port', 'ssh_user', 'ssh_password', 'remote_host',
                'remote_port', 'database', 'user', 'password', 'identity_file', 'known_hosts'):
        value = os.environ.get('XIEHE_DICTIONARY_' + key.upper())
        if value is not None:
            cfg[key] = value
    if not cfg.get('password'):
        raise ValueError('缺少数据库密码；通过 Skill 外的 XIEHE_DICTIONARY_CONFIG 或 XIEHE_DICTIONARY_PASSWORD 提供')
    for key in ('ssh_host', 'ssh_user', 'remote_host', 'database', 'user'):
        value = cfg.get(key)
        if not isinstance(value, str) or not value or value.startswith('-') or any(c.isspace() for c in value):
            raise ValueError('连接参数无效：' + key)
    for key in ('ssh_port', 'remote_port'):
        cfg[key] = int(cfg[key])
        if not 1 <= cfg[key] <= 65535:
            raise ValueError('连接端口无效：' + key)
    return cfg


@contextmanager
def tunnel(cfg):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='xiehe-dictionary-ssh-') as tmp:
        env = os.environ.copy()
        command = ['ssh', '-N', '-T', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                   '-o', 'ControlPersist=no', '-o', 'ExitOnForwardFailure=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
        if cfg.get('identity_file'):
            command += ['-i', str(Path(cfg['identity_file']).expanduser())]
        if cfg.get('known_hosts'):
            command += ['-o', 'UserKnownHostsFile=' + str(Path(cfg['known_hosts']).expanduser())]
        if cfg.get('ssh_password'):
            askpass = Path(tmp) / 'askpass'
            askpass.write_text('#!/bin/sh\nprintf \'%s\\n\' "$XIEHE_DICTIONARY_SSH_PASSWORD"\n')
            askpass.chmod(0o700)
            env.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE='force', DISPLAY=':0',
                       XIEHE_DICTIONARY_SSH_PASSWORD=cfg['ssh_password'])
            command += ['-o', 'NumberOfPasswordPrompts=1', '-o', 'PreferredAuthentications=publickey,password']
        else:
            command += ['-o', 'BatchMode=yes']
        command += ['-L', f"127.0.0.1:{port}:{cfg['remote_host']}:{cfg['remote_port']}",
                    '-p', str(cfg['ssh_port']), '-l', cfg['ssh_user'], cfg['ssh_host']]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    error = process.stderr.read().decode(errors='replace')
                    reason = ('主机密钥未核验' if 'Host key verification failed' in error else
                              'SSH认证失败' if 'Permission denied' in error else
                              '隧道转发被服务端拒绝' if 'administratively prohibited' in error else
                              '连接配置或SSH访问失败')
                    raise RuntimeError('SSH 隧道未建立：' + reason + '；检查内网/VPN和外部连接配置')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.25):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError('SSH 隧道连接超时')
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stderr:
                process.stderr.close()


def fetch():
    import psycopg
    from psycopg.rows import dict_row
    cfg = connection_config()
    with tunnel(cfg) as port:
        with psycopg.connect(host='127.0.0.1', port=port, dbname=cfg['database'], user=cfg['user'],
                             password=cfg['password'], connect_timeout=10, sslmode='disable',
                             options='-c default_transaction_read_only=on -c statement_timeout=15000',
                             row_factory=dict_row) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            readonly = conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']
            if readonly != 'on':
                raise ValueError('数据库未处于只读事务')
            tables, columns = {}, {}
            for view in VIEWS:
                # VIEWS is a fixed allowlist, never supplied by the server or caller.
                cursor = conn.execute(f'SELECT * FROM dictionary_readonly.{view} LIMIT 20001')
                columns[view] = [col.name for col in cursor.description]
                rows = cursor.fetchall()
                if len(rows) > 20000:
                    raise ValueError('字典视图超过支持的 20000 行；停止，不能声称拉取完整')
                tables[view] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return {'schema_version': 1, 'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'source': {'transport': 'ssh_postgresql', 'ssh_host': cfg['ssh_host'],
                       'ssh_port': cfg['ssh_port'], 'database': cfg['database'],
                       'schema': 'dictionary_readonly', 'transaction_read_only': True},
            'columns': columns, 'tables': tables}


if __name__ == '__main__':
    try:
        print(json.dumps(fetch(), ensure_ascii=False, default=str))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
    except Exception:
        # Driver errors may include connection strings. Never echo them or credentials.
        print(json.dumps({'error': '字典只读查询失败；检查 psycopg 依赖、数据库认证、视图权限和服务状态'}, ensure_ascii=False))
        raise SystemExit(2)
