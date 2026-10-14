"""Offline mount boundary regression. Docker is never executed by these tests."""
import os
from pathlib import Path, PurePosixPath
import re

import pytest
import yaml

from tools import container_watch as c

ROOT = Path(__file__).resolve().parents[2]
REPO_HOST = PurePosixPath('/opt/local_cache_crypt/_projects/takeout-downloader')


def mount(source, *, kind='bind', readonly=False, target='/safe'):
    return {'Type': kind, 'Source': source, 'Destination': target, 'RW': not readonly}


def expand(value, env):
    return re.sub(r'\$\{([^:}]+):-([^}]*)\}', lambda m: env.get(m[1]) or m[2], value)


def compose_mounts(service, env):
    for item in service.get('volumes', []):
        if isinstance(item, str):
            source, target, *options = item.split(':')
            readonly = 'ro' in options
        else:
            assert item['type'] == 'bind'
            source, target = item['source'], item['target']
            readonly = item.get('read_only', False)
        source, target = expand(source, env), expand(target, env)
        if not source.startswith('/'):
            source = str(REPO_HOST / source)
        yield mount(source, target=target, readonly=readonly)


@pytest.mark.parametrize('unsafe_root', [None, '/', '/opt', '/opt/local_cache_crypt', c.HOST_SECRETS])
def test_all_shipped_compose_defaults_exclude_host_secrets_and_fix_archives(unsafe_root):
    files = sorted(ROOT.glob('docker-compose*.yml'))
    assert {p.name for p in files} == {'docker-compose.yml', 'docker-compose.webgui.yml'}
    env = {} if unsafe_root is None else {'STORAGE_ROOT': unsafe_root, 'ALLOWED_DIRS': '/'}
    for file in files:
        services = yaml.safe_load(file.read_text(encoding='utf-8'))['services']
        for name, service in services.items():
            mounts = list(compose_mounts(service, env))
            if name == 'cloudflared':
                assert not mounts
                continue
            c.validate_mounts(mounts)
            archive = [m for m in mounts if m['Destination'] == '/opt/archives']
            assert archive == [mount('/opt/archives', target='/opt/archives')]
            assert not any(m['Source'] in {'/', '/opt', '/opt/local_cache_crypt'} for m in mounts)
            assert any(m['Destination'] == '/work' and m['Source'] == str(REPO_HOST) for m in mounts)
            if name == 'webgui':
                assert service['environment']['STORAGE_ROOT'] == '/opt/archives'
                assert mount(str(REPO_HOST / 'config'), target='/config') in mounts
                assert mount('/opt/local_cache_crypt/rclone_vfs', target='/var/rclone_vfs', readonly=True) in mounts
                assert mount('/opt/storage.jfs002', target='/opt/storage.jfs002', readonly=True) in mounts
            else:
                assert service['environment']['ALLOWED_DIRS'] == '/opt/archives:/downloads'
                assert mount(str(REPO_HOST / 'downloads'), target='/downloads') in mounts
                assert mount(str(REPO_HOST / 'drop'), target='/downloads/drop') in mounts
            # Fixed archive source and target may not depend on ANY environment override.
            raw_archive = [m for m in service['volumes'] if isinstance(m, dict)
                           and m.get('target') == '/opt/archives']
            assert len(raw_archive) == 1 and raw_archive[0]['source'] == '/opt/archives'


@pytest.mark.parametrize('source', ['/', '/opt', '/opt/local_cache_crypt', c.HOST_SECRETS,
                                    c.HOST_SECRETS + '/credentials.env', '/opt/archives/../local_cache_crypt'])
@pytest.mark.parametrize('kind', ['bind', 'volume'])
@pytest.mark.parametrize('readonly', [False, True])
def test_ancestors_exact_and_children_rejected_even_readonly(source, kind, readonly):
    with pytest.raises(c.Refused):
        c.validate_mounts([mount(source, kind=kind, readonly=readonly)])


@pytest.mark.parametrize('data', [None, {}, '', [], [None], [{}], ['SECRET'],
    [mount('relative')], [mount('')], [mount('//opt')], [mount('/opt\x00SECRET')],
    [mount('/safe', kind='unknown')], [mount('/safe', kind=['bind'])],
    [{k: v for k, v in mount('/safe').items() if k != 'Source'}],
    [{k: v for k, v in mount('/safe').items() if k != 'Destination'}],
    [{k: v for k, v in mount('/safe').items() if k != 'RW'}],
    [dict(mount('/safe'), RW='false')], [dict(mount('/safe'), Source=123)],
    [dict(mount('/safe'), Destination=None)], [mount('/safe', kind='tmpfs')]])
def test_missing_or_malformed_metadata_refuses(data, capsys):
    with pytest.raises(c.Refused) as exc:
        c.validate_mounts(data)
    assert 'SECRET' not in str(exc.value)
    assert capsys.readouterr() == ('', '')


def test_safe_actual_layout_fixture_permits():
    # Synthetic transcription of reported specific mounts; NOT a live observation.
    c.validate_mounts([
        mount(str(REPO_HOST), target='/work'),
        mount(str(REPO_HOST / 'config'), target='/config'),
        mount('/opt/archives', target='/opt/archives'),
        mount('/opt/local_cache_crypt/rclone_vfs', target='/var/rclone_vfs', readonly=True),
        mount('/opt/storage.jfs002', target='/opt/storage.jfs002', readonly=True),
    ])
    c.validate_mounts([mount(c.HOST_SECRETS + '-sibling')])
    c.validate_mounts([mount('', kind='tmpfs')])


@pytest.mark.skipif(os.name == 'nt', reason='real symlinks require Linux fixture')
@pytest.mark.parametrize('ancestor', ['root', 'opt', 'encrypted', 'secrets'])
@pytest.mark.parametrize('readonly', [False, True])
def test_real_symlink_source_ancestors_refuse(tmp_path, ancestor, readonly):
    root = tmp_path / 'host'
    secret = root / 'opt' / 'encrypted' / 'secrets'
    secret.mkdir(parents=True)
    levels = {'root': root, 'opt': secret.parents[1], 'encrypted': secret.parent, 'secrets': secret}
    alias = tmp_path / 'alias'
    alias.symlink_to(levels[ancestor], target_is_directory=True)
    with pytest.raises(c.Refused):
        c.validate_mounts([mount(str(alias), readonly=readonly)], protected=str(secret))
    # A path through a symlinked parent must also be resolved.
    with pytest.raises(c.Refused):
        c.validate_mounts([mount(str(alias / secret.relative_to(levels[ancestor])))], protected=str(secret))


@pytest.mark.skipif(os.name == 'nt', reason='real symlinks require Linux fixture')
def test_protected_symlink_and_resolution_failure_refuse(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    protected = tmp_path / 'protected'
    protected.symlink_to(real, target_is_directory=True)
    with pytest.raises(c.Refused):
        c.validate_mounts([mount(str(real))], protected=str(protected))
    loop = tmp_path / 'loop'
    loop.symlink_to(loop)
    with pytest.raises(c.Refused):
        c.validate_mounts([mount(str(loop))], protected=str(protected))
