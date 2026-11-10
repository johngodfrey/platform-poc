#!/usr/bin/env python3
"""Render the local OAM manifest without evaluating secrets as shell code."""
import json
from pathlib import Path
import re
import sys


def main():
    directory = Path(__file__).resolve().parent
    secret_file = directory / '.env.secret'
    if not secret_file.is_file():
        sys.exit('Copy .env.secret.example to .env.secret and set your PostgreSQL credentials.')
    values = {}
    for line in secret_file.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator:
            sys.exit('Invalid .env.secret entry: expected KEY=value.')
        values[key.strip()] = value
    for key in ('POSTGRES_USERNAME', 'POSTGRES_PASSWORD', 'POSTGRES_DATABASE'):
        if not values.get(key) or values[key] == 'CHANGE_ME':
            sys.exit(f'Set {key} in .env.secret before deploying.')
    template = (directory / 'wadm.yaml').read_text()

    def substitute(match):
        key = match.group(1)
        if key not in values:
            sys.exit(f'Missing configuration key: {key}')
        # JSON strings are valid YAML scalars; preserve punctuation safely.
        return json.dumps(values[key])

    rendered = re.sub(r'\$\{([A-Z_]+)\}', substitute, template)
    output_directory = directory / '.generated'
    output_directory.mkdir(mode=0o700, exist_ok=True)
    output_directory.chmod(0o700)
    output = output_directory / 'wadm.yaml'
    # Restrict access before writing any secret content.
    output.touch(mode=0o600, exist_ok=True)
    output.chmod(0o600)
    output.write_text(rendered)


if __name__ == '__main__':
    main()
