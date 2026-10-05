"""Install this checkout's hooks, MCP registration, and standing instructions."""
import json
import os
import re
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from optchat.adapters import hook_config, mcp_command
from optchat.compactor import OPTCHAT_GUIDANCE, VIEW_DOC
from optchat.daemon import DEFAULT_CONFIG

USER = Path.home()
MEMORY = USER / '.optchat'
STAMP = datetime.now().strftime('%Y%m%d-%H%M%S')
BACKUP = MEMORY / 'install-backups' / STAMP
START, END = '<!-- optchat:start -->', '<!-- optchat:end -->'
SECTION = START + '\n' + OPTCHAT_GUIDANCE + '\n' + VIEW_DOC + END


def backup(path):
    if path.exists():
        dest = BACKUP / path.relative_to(USER)
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, dest)
        os.chmod(dest, 0o600)


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    temp = path.with_name(path.name + '.optchat-tmp')
    with temp.open('w') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    temp.replace(path)
    print('Updated', path)


def section(text):
    if START in text:
        return re.sub(re.escape(START) + r'.*?' + re.escape(END), lambda _: SECTION, text, flags=re.S)
    return text.rstrip() + '\n\n' + SECTION + '\n'


def json_file(path):
    return json.loads(path.read_text()) if path.exists() else {}


def install_hooks(path, agent):
    document = json_file(path)
    hooks = document.setdefault('hooks', {})
    fragment = hook_config(agent, MEMORY)
    for event, groups in fragment['hooks'].items():
        # Replace only our prior handlers; retain every unrelated matcher/handler.
        preserved = []
        for group in hooks.get(event, []):
            handlers = [handler for handler in group.get('hooks', [])
                        if not ('from optchat.cli import main' in handler.get('command', '')
                                and ' hook --agent ' in handler.get('command', ''))]
            if handlers:
                preserved.append({**group, 'hooks': handlers})
        hooks[event] = preserved + groups
    write(path, json.dumps(document, ensure_ascii=False, indent=2) + '\n')


MEMORY.mkdir(mode=0o700, parents=True, exist_ok=True)
if not (MEMORY / 'config.json').exists():
    write(MEMORY / 'config.json', json.dumps({**DEFAULT_CONFIG, 'claude_binary': shutil.which('claude') or 'claude'}, indent=2) + '\n')
install_hooks(USER / '.codex/hooks.json', 'codex')
install_hooks(USER / '.claude/settings.json', 'claude')

config_path = USER / '.codex/config.toml'
text = config_path.read_text() if config_path.exists() else ''
parsed = tomllib.loads(text)
old = parsed.get('developer_instructions', '')
value = 'developer_instructions = ' + json.dumps(section(old), ensure_ascii=False) + '\n'
if 'developer_instructions' in parsed:
    pattern = r'''(?m)^developer_instructions\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*(?:#[^\n]*)?$'''
    text, replaced = re.subn(pattern, lambda _: value.rstrip(), text, count=1)
    if replaced != 1:
        raise RuntimeError('Existing multiline developer_instructions: refusing to replace it unsafely')
else:
    text = value + '\n' + text
command, args = mcp_command(MEMORY)
existing_codex = parsed.get('mcp_servers', {}).get('optchat')
if existing_codex and (existing_codex.get('command') != command or existing_codex.get('args') != args):
    raise RuntimeError('A different OptChat MCP server is already registered; inspect before overwriting')
if not existing_codex:
    text += '\n[mcp_servers.optchat]\ncommand = ' + json.dumps(command) + '\nargs = ' + json.dumps(args) + '\nstartup_timeout_sec = 30\n'
tomllib.loads(text)
write(config_path, text)

claude_path = USER / '.claude/CLAUDE.md'
write(claude_path, section(claude_path.read_text() if claude_path.exists() else ''))
claude_mcp = USER / '.claude.json'
document = json_file(claude_mcp)
existing_claude = document.get('mcpServers', {}).get('optchat')
if existing_claude and (existing_claude.get('command') != command or existing_claude.get('args') != args):
    raise RuntimeError('A different OptChat Claude MCP server is already registered; inspect before overwriting')
document.setdefault('mcpServers', {})['optchat'] = {'type': 'stdio', 'command': command, 'args': args}
write(claude_mcp, json.dumps(document, ensure_ascii=False, indent=2) + '\n')

launcher = USER / '.local/bin/optchat'
launcher.parent.mkdir(parents=True, exist_ok=True)
target = ROOT / '.venv/bin/optchat'
if launcher.exists() or launcher.is_symlink():
    if not launcher.is_symlink() or launcher.resolve() != target.resolve():
        raise RuntimeError('An unrelated optchat executable already exists; refusing to replace it')
else:
    launcher.symlink_to(target)
print('CLI', launcher)
print('Backups', BACKUP)
