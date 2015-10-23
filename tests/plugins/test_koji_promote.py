"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

try:
    import koji
except ImportError:
    import inspect
    import os
    import sys

    # Find out mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji

try:
    from atomic_reactor.plugins.post_push_to_pulp import PulpPushPlugin
    PULP_PUSH_KEY = PulpPushPlugin.key
except (ImportError, SyntaxError):
    PULP_PUSH_KEY = None

from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.inner import DockerBuildWorkflow, TagConf
from atomic_reactor.util import ImageName
from atomic_reactor.source import GitSource, PathSource
from tests.constants import SOURCE, MOCK

from flexmock import flexmock
import pytest
from tests.docker_mock import mock_docker
import subprocess
from osbs.api import OSBS
from six import string_types


class X(object):
    pass


class MockedPodResponse(object):
    def get_container_image_ids(self):
        return {'buildroot:latest': '0123456'}


class MockedClientSession(object):
    def __init__(self, hub):
        self.uploaded_files = []

    def krb_login(self, proxyuser=None):
        pass

    def ssl_login(self, cert, ca, serverca, proxyuser=None):
        pass

    def logout(self):
        pass

    def uploadWrapper(self, localfile, path, name=None, callback=None,
                      blocksize=1048576, overwrite=True):
        self.uploaded_files.append(path)

    def CGImport(self, metadata, server_dir):
        self.metadata = metadata
        self.server_dir = server_dir


FAKE_RPM_OUTPUT = (b'name1;1.0;1;x86_64;0;01234567;(none);RSA/SHA256, Mon 29 Jun 2015 13:58:22 BST, Key ID abcdef01234567\n'
                   b'gpg-pubkey;01234567;01234567;(none);(none);(none);(none);(none)\n'
                   b'gpg-pubkey-doc;01234567;01234567;noarch;(none);(none);(none);(none)\n'
                   b'name2;2.0;2;x86_64;0;12345678;RSA/SHA256, Mon 29 Jun 2015 13:58:22 BST, Key ID bcdef012345678;(none)\n\n')

FAKE_OS_OUTPUT = 'fedora-22'


def fake_subprocess_output(cmd):
    if cmd.startswith('/bin/rpm'):
        return FAKE_RPM_OUTPUT
    elif 'os-release' in cmd:
        return FAKE_OS_OUTPUT
    else:
        raise RuntimeError


class MockedPopen(object):
    def __init__(self, cmd, *args, **kwargs):
        self.cmd = cmd

    def wait(self):
        return 0

    def communicate(self):
        return (fake_subprocess_output(self.cmd), '')


def fake_Popen(cmd, *args, **kwargs):
    return MockedPopen(cmd, *args, **kwargs)




def is_string_type(obj):
    return any(isinstance(obj, strtype)
               for strtype in string_types)


def check_components(components):
    assert isinstance(components, list)
    assert len(components) > 0
    for component_rpm in components:
        assert isinstance(component_rpm, dict)
        assert 'type' in component_rpm
        assert component_rpm['type'] == 'rpm'
        assert 'name' in component_rpm
        assert component_rpm['name']
        assert is_string_type(component_rpm['name'])
        assert component_rpm['name'] != 'gpg-pubkey'
        assert 'version' in component_rpm
        assert component_rpm['version']
        assert is_string_type(component_rpm['version'])
        assert 'release' in component_rpm
        assert component_rpm['release']
        assert 'epoch' in component_rpm
        assert 'arch' in component_rpm
        assert is_string_type(component_rpm['arch'])
        assert 'sigmd5' in component_rpm
        assert 'signature' in component_rpm
        assert component_rpm['signature'] != '(none)'


def prepare(tmpdir, session=None, name=None, version=None, release=None,
            source=None, build_process_failed=False, is_rebuild=True,
            ssl_certs=False):
    if session is None:
        session = MockedClientSession('')
    if source is None:
        source = GitSource('git', 'git://hostname/path')

    build_id = 'build-1'
    namespace = 'mynamespace'
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    base_image_id = '123456parent-id'
    setattr(workflow, '_base_image_inspect', {'Id': base_image_id})
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', '123456imageid')
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='22'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder, 'built_image_info', {'ParentId': base_image_id})
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', TagConf())
    if name and version:
        workflow.tag_conf.add_unique_image('user/{n}:{v}-timestamp'
                                           .format(n=name,
                                                   v=version))
    if name and version and release:
        workflow.tag_conf.add_primary_images(["{0}:{1}-{2}".format(name,
                                                                   version,
                                                                   release),
                                              "{0}:{1}".format(name, version),
                                              "{0}:latest".format(name)])

    flexmock(subprocess, Popen=fake_Popen)
    flexmock(koji, ClientSession=lambda hub: session)
    flexmock(GitSource)
    (flexmock(OSBS)
        .should_receive('get_build_logs')
        .with_args(build_id, namespace=namespace)
        .and_return('build logs'))
    (flexmock(OSBS)
        .should_receive('get_pod_for_build')
        .with_args(build_id, namespace=namespace)
        .and_return(MockedPodResponse()))
    setattr(workflow, 'source', source)
    setattr(workflow.source, 'lg', X())
    setattr(workflow.source.lg, 'commit_id', '123456')
    setattr(workflow, 'build_logs', ['docker build log\n'])
    setattr(workflow, 'postbuild_results', {})
    if PULP_PUSH_KEY is not None:
        workflow.postbuild_results[PULP_PUSH_KEY] = [
            ImageName(registry='registry.example.com', namespace='namespace',
                      repo='repo', tag='tag')
        ]

    with open(os.path.join(str(tmpdir), 'image.tar.xz'), 'wt') as fp:
        fp.write('x' * 2**12)
        setattr(workflow, 'exported_image_sequence', [{'path': fp.name}])

    setattr(workflow, 'build_failed', build_process_failed)
    workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = is_rebuild
    workflow.postbuild_results[PostBuildRPMqaPlugin.key] = [
        "name1,1.0,1,x86_64,0,2000,01234567,23000",
        "name2,2.0,1,x86_64,0,3000,abcdef01,24000",
    ]

    args = {
        'kojihub': '',
        'url': '/',
    }
    if ssl_certs:
        args['koji_ssl_certs'] = '/'

    runner = ExitPluginsRunner(tasker, workflow,
                                    [
                                        {
                                            'name': KojiPromotePlugin.key,
                                            'args': args,
                                        },
                                    ])

    os.environ.update({
        'BUILD': json.dumps({
            "metadata": {
                "creationTimestamp": "2015-07-27T09:24:00Z",
                "namespace": namespace,
                "name": build_id,
            }
        }),
        'OPENSHIFT_CUSTOM_BUILD_BASE_IMAGE': 'buildroot:latest',
    })

    return runner


@pytest.mark.skipif(PULP_PUSH_KEY is None,
                    reason="plugin requires push_pulp")
class TestKojiPromote(object):
    def test_koji_promote_failed_build(self, tmpdir):
        session = MockedClientSession('')
        runner = prepare(tmpdir, build_process_failed=True,
                         name='name', version='1.0', release='1')
        runner.run()

        # Must not have promoted this build
        assert not hasattr(session, 'metadata')

    def test_koji_promote_not_rebuild(self, tmpdir):
        session = MockedClientSession('')
        runner = prepare(tmpdir, session, is_rebuild=False, name='name',
                         version='1.0', release='1')
        runner.run()

        # Must not have promoted this build
        assert not hasattr(session, 'metadata')

    def test_koji_promote_no_tagconf(self, tmpdir):
        runner = prepare(tmpdir)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_promote_no_build_env(self, tmpdir):
        runner = prepare(tmpdir, name='name', version='1.0', release='1')

        # No BUILD environment variable
        if "BUILD" in os.environ:
            del os.environ["BUILD"]
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_promote_no_build_metadata(self, tmpdir):
        runner = prepare(tmpdir, name='name', version='1.0', release='1')

        # No BUILD metadata
        os.environ["BUILD"] = json.dumps({})
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_promote_invalid_creation_timestamp(self, tmpdir):
        runner = prepare(tmpdir, name='name', version='1.0', release='1')

        # Invalid timestamp format
        os.environ["BUILD"] = json.dumps({
            "metadata": {
                "creationTimestamp": "2015-07-27 09:24 UTC"
            }
        })
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_promote_wrong_source_type(self, tmpdir):
        runner = prepare(tmpdir, name='name', version='1.0', release='1',
                         source=PathSource('path', 'file:///dev/null'))
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_promote_krb_fail(self, tmpdir):
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('krb_login')
            .and_raise(RuntimeError)
            .once())
        name = 'name'
        version = '1.0'
        release = '1'
        runner = prepare(tmpdir, session, name=name, version=version,
                         release=release)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_promote_ssl_fail(self, tmpdir):
        session = MockedClientSession('')
        (flexmock(session)
            .should_receive('ssl_login')
            .and_raise(RuntimeError)
            .once())
        name = 'name'
        version = '1.0'
        release = '1'
        runner = prepare(tmpdir, session, name=name, version=version,
                         release=release, ssl_certs=True)
        with pytest.raises(PluginFailedException):
            runner.run()

    def test_koji_promote_success(self, tmpdir):
        session = MockedClientSession('')
        name = 'name'
        version = '1.0'
        release = '1'
        runner = prepare(tmpdir, session, name=name, version=version,
                         release=release)
        runner.run()

        data = session.metadata
        assert set(data.keys()) == set([
            'metadata_version',
            'build',
            'buildroots',
            'output',
        ])

        assert data['metadata_version'] in ['0', 0]

        build = data['build']
        assert isinstance(build, dict)

        buildroots = data['buildroots']
        assert isinstance(buildroots, list)
        assert len(buildroots) > 0

        output_files = data['output']
        assert isinstance(output_files, list)

        assert set(build.keys()) == set([
            'name',
            'version',
            'release',
            'source',
            'start_time',
            'end_time',
            'extra',
        ])

        assert build['name'] == name
        assert build['version'] == version
        assert build['release'] == release
        assert build['source'] == 'git://hostname/path#123456'
        assert int(build['start_time']) > 0
        assert int(build['end_time']) > 0
        extra = build['extra']
        assert isinstance(extra, dict)

        for buildroot in buildroots:
            assert isinstance(buildroot, dict)

            assert set(buildroot.keys()) == set([
                'id',
                'host',
                'content_generator',
                'container',
                'tools',
                'components',
                'extra',
            ])

            # Unique within buildroots in this metadata
            assert len([b for b in buildroots
                        if b['id'] == buildroot['id']]) == 1

            host = buildroot['host']
            assert isinstance(host, dict)
            assert set(host.keys()) == set([
                'os',
                'arch',
            ])

            assert host['os']
            assert is_string_type(host['os'])
            assert host['arch']
            assert is_string_type(host['arch'])
            assert host['arch'] != 'amd64'

            content_generator = buildroot['content_generator']
            assert isinstance(content_generator, dict)
            assert set(content_generator.keys()) == set([
                'name',
                'version',
            ])

            assert content_generator['name']
            assert is_string_type(content_generator['name'])
            assert content_generator['version']
            assert is_string_type(content_generator['version'])

            container = buildroot['container']
            assert isinstance(container, dict)
            assert set(container.keys()) == set([
                'type',
                'arch',
            ])

            assert container['type'] == 'docker'
            assert container['arch']
            assert is_string_type(container['arch'])

            assert isinstance(buildroot['tools'], list)
            assert len(buildroot['tools']) > 0
            for tool in buildroot['tools']:
                assert isinstance(tool, dict)
                assert set(tool.keys()) == set([
                    'name',
                    'version',
                ])

                assert tool['name']
                assert is_string_type(tool['name'])
                assert tool['version']
                assert is_string_type(tool['version'])

            check_components(buildroot['components'])

            extra = buildroot['extra']
            assert isinstance(extra, dict)
            assert set(extra.keys()) == set([
                'osbs',
            ])

            assert 'osbs' in extra
            osbs = extra['osbs']
            assert isinstance(osbs, dict)
            assert set(osbs.keys()) == set([
                'build_id',
                'builder_image_id',
            ])

            assert is_string_type(osbs['build_id'])
            assert is_string_type(osbs['builder_image_id'])

        for output in output_files:
            assert isinstance(output, dict)
            assert 'type' in output
            assert 'buildroot_id' in output
            buildroot_id = output['buildroot_id']
            # References one of the buildroots
            assert len([buildroot for buildroot in buildroots
                        if buildroot['id'] == buildroot_id]) == 1
            assert 'filename' in output
            assert output['filename']
            assert is_string_type(output['filename'])
            assert 'filesize' in output
            assert int(output['filesize']) > 0
            assert 'arch' in output
            assert output['arch']
            assert is_string_type(output['arch'])
            assert 'checksum' in output
            assert output['checksum']
            assert is_string_type(output['checksum'])
            assert 'checksum_type' in output
            assert output['checksum_type'] == 'md5'
            assert is_string_type(output['checksum_type'])
            assert 'type' in output
            if output['type'] == 'log':
                assert set(output.keys()) == set([
                    'buildroot_id',
                    'filename',
                    'filesize',
                    'arch',
                    'checksum',
                    'checksum_type',
                    'type',
                ])
                assert output['arch'] == 'noarch'
            else:
                assert set(output.keys()) == set([
                    'buildroot_id',
                    'filename',
                    'filesize',
                    'arch',
                    'checksum',
                    'checksum_type',
                    'type',
                    'components',
                    'extra',
                ])
                assert output['type'] == 'docker-image'
                assert is_string_type(output['arch'])
                assert output['arch'] != 'noarch'
                check_components(output['components'])

                extra = output['extra']
                assert isinstance(extra, dict)
                assert set(extra.keys()) == set([
                    'image',
                    'docker',
                ])

                image = extra['image']
                assert isinstance(image, dict)
                assert set(image.keys()) == set([
                    'arch',
                ])

                assert image['arch'] == output['arch']  # what else?

                assert 'docker' in extra
                docker = extra['docker']
                assert isinstance(docker, dict)
                assert set(docker.keys()) == set([
                    'parent_id',
                    'tag',
                    'id',
                    'destination_repo',
                ])

                assert is_string_type(docker['parent_id'])
                assert is_string_type(docker['tag'])
                assert is_string_type(docker['id'])
                assert is_string_type(docker['destination_repo'])

        files = session.uploaded_files

        # There should be a file in the list for each output
        assert isinstance(files, list)
        assert len(files) == len(output_files)