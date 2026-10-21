"""Check archived approval consistency without calling the Actions API."""
import argparse
import hashlib
import runpy
import zipfile
from pathlib import Path

B = runpy.run_path(str(Path(__file__).with_name('evidence-bundle.py')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    try:
        path = Path(args.file)
        B['require'](path.stat().st_size <= B['MAX_BUNDLE'], 'bundle exceeds limit')
        raw = path.read_bytes()
        if args.expected_sha256:
            B['require'](hashlib.sha256(raw).hexdigest() == args.expected_sha256, 'external digest mismatch')
        bundle = B['parse_json'](raw)
        result = B['verify_bundle'](bundle, args.repository, args.tag, args.candidate)
        print(B['canonical'](result).decode(), end='')
        return 0
    except (KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile) as error:
        print('Evidence verification failed: ' + str(error), file=__import__('sys').stderr)
        return 2 if 'unsupported' in str(error) else 1


if __name__ == '__main__':
    raise SystemExit(main())
