"""Actions should preserve known SSH endpoints when a transfer URL omits its port."""
from types import SimpleNamespace

import pytest

from ngts.cli_wrappers.nvue.nvue_system_clis import NvueSystemCli
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra import BaseComponent as component_module
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit


@pytest.mark.parametrize('url,expected', [
    ('scp://root:secret@192.0.2.10/tmp/file', 'scp://root:secret@192.0.2.10:2222/tmp/file'),
    ('sftp://root:secret@192.0.2.10/tmp/file', 'sftp://root:secret@192.0.2.10:2222/tmp/file'),
    ('scp://root:secret@[2001:db8::10]/tmp/file', 'scp://root:secret@[2001:db8::10]:2222/tmp/file'),
    ('scp://root:%73ecret@192.0.2.10/tmp/file', 'scp://root:%73ecret@192.0.2.10:2222/tmp/file'),
    ('scp://root:secret@192.0.2.10:2200/tmp/file', 'scp://root:secret@192.0.2.10:2200/tmp/file'),
    ('scp://root:secret@192.0.2.10:22/tmp/file', 'scp://root:secret@192.0.2.10:22/tmp/file'),
    ('scp://root:secret@192.0.2.99/tmp/file', 'scp://root:secret@192.0.2.99/tmp/file'),
    ('scp://other:secret@192.0.2.10/tmp/file', 'scp://other:secret@192.0.2.10/tmp/file'),
    ('scp://root:wrong@192.0.2.10/tmp/file', 'scp://root:wrong@192.0.2.10/tmp/file'),
    ('scp://root:secret192.0.2.10/tmp/file', 'scp://root:secret192.0.2.10/tmp/file'),
    ('scp://root:secret@192.0.2.10:/tmp/file', 'scp://root:secret@192.0.2.10:/tmp/file'),
    ('scp://root:secret@192.0.2.10:bad/tmp/file', 'scp://root:secret@192.0.2.10:bad/tmp/file'),
    ('https://root:secret@192.0.2.10/tmp/file', 'https://root:secret@192.0.2.10/tmp/file'),
])
def test_action_fills_only_missing_known_ssh_port(monkeypatch, url, expected):
    commands = []
    remote = SimpleNamespace(ip='192.0.2.10', switch_reachable_ip='2001:db8::10', ssh_port=2222,
                             username='root', password='secret')
    monkeypatch.setattr(TestToolkit, 'engines', {'sonic_mgmt': remote})
    monkeypatch.setattr(component_module, 'check_port_status_till_alive', lambda *args, **kwargs: None)
    connection = SimpleNamespace(send_command_timing=lambda command, **kwargs: commands.append(command) or
                                 ('0' if command == 'echo $?' else 'Action succeeded'))
    dut = SimpleNamespace(ip='192.0.2.2', ssh_port=22, engine=connection)
    component = BaseComponent(path='/system/image', api={ApiType.NVUE: NvueSystemCli}, force_api=ApiType.NVUE)

    component.action('fetch', ('remote-url', url), engine=dut, device=SimpleNamespace(),
                     expected_output='Action succeeded').verify_result()

    assert commands[0] == f'nv action fetch system image {expected}'


@pytest.mark.parametrize('ports', [[], [22], [None], [2222, 3333]])
def test_default_unknown_or_ambiguous_endpoint_is_not_rewritten(monkeypatch, ports):
    remotes = {str(index): SimpleNamespace(ip='192.0.2.10', ssh_port=port, username='root', password='secret')
               for index, port in enumerate(ports)}
    monkeypatch.setattr(TestToolkit, 'engines', remotes)
    url = 'scp://root:secret@192.0.2.10/tmp/file'

    assert BaseComponent._add_missing_ssh_port(url) == url
