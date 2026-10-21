"""Offline documentation syntax and local-file links; no network probes."""
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path.cwd()
FENCE = chr(96) * 3


def verify(path):
    source = path.read_text()
    for language, body in re.findall('^' + FENCE + r'(yaml|yml|json)\s*\n(.*?)^' + FENCE + r'\s*$', source, re.M | re.S):
        if language == 'json':
            json.loads(body)
        else:
            result = subprocess.run(['ruby', '-ryaml', '-e', 'YAML.safe_load(STDIN.read, aliases: true)'],
                                    input=body, capture_output=True, text=True)
            if result.returncode:
                raise ValueError(f'{path}: invalid YAML example')
    prose = re.sub('^' + FENCE + r'[^\n]*\n.*?^' + FENCE + r'\s*$', '', source, flags=re.M | re.S)
    for target in re.findall(r'\]\(([^\s)]+)\)', prose):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target_path = (path.parent / unquote(parsed.path)).resolve()
        if not target_path.is_relative_to(ROOT.resolve()) or not target_path.exists():
            raise ValueError(f'{path}: missing local link {target}')


if __name__ == '__main__':
    files = [ROOT/'README.md', ROOT/'CONTRIBUTING.md', ROOT/'prepare/README.md', ROOT/'distribute/README.md']
    files += sorted((ROOT/'docs').rglob('*.md'))
    for path in files:
        if path.is_file():
            verify(path)
    print('Documentation syntax and local file links verified')
