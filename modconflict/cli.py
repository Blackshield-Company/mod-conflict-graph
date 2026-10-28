"""Command-line orchestration; no network or external dependencies."""
import argparse
from pathlib import Path
import sys

from .analyze import analyze, SEVERITIES
from .extract import build_mod, InputError
from .render import graph_data, write


def parser():
    p = argparse.ArgumentParser(description='Build an evidence-based Skyrim SE mod conflict graph.')
    p.add_argument('--clones', type=Path)
    p.add_argument('--releases', type=Path)
    p.add_argument('--mods', help='Comma-separated repository names')
    p.add_argument('--extra', default='', help='Comma-separated repos outside the primary set')
    p.add_argument('--out', type=Path, default=Path('out'))
    p.add_argument('--fail-on', choices=('collision', 'probable', 'never'), default='never')
    p.add_argument('--json-only', action='store_true')
    p.add_argument('--quiet', action='store_true')
    p.add_argument('--full', action='store_true', help='Include full record facts (never raw payloads)')
    p.add_argument('--no-timestamp', action='store_true')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not args.clones and not args.releases:
        p.error('at least one of --clones and --releases is required')
    for root in (args.clones, args.releases):
        if root is not None and not root.is_dir():
            p.error('input directory does not exist: %s' % root)
    discovered = set()
    try:
        for root in (args.clones, args.releases):
            if root:
                discovered.update(d.name for d in root.iterdir() if d.is_dir() and not d.is_symlink() and not d.name.startswith('.'))
    except OSError as exc:
        print('mod-conflict-graph: %s' % exc, file=sys.stderr)
        return 2
    selected = {x.strip() for x in args.mods.split(',') if x.strip()} if args.mods is not None else discovered
    extras = {x.strip() for x in args.extra.split(',') if x.strip()}
    selected |= extras
    unknown = selected - discovered
    if unknown:
        p.error('unknown mods: %s' % ', '.join(sorted(unknown)))
    if not selected:
        p.error('no mods found or selected')
    mods = []
    try:
        for name in sorted(selected, key=str.casefold):
            clone = args.clones / name if args.clones and (args.clones / name).is_dir() else None
            release = args.releases / name if args.releases and (args.releases / name).is_dir() else None
            mods.append(build_mod(name, clone, release, name in extras))
        analysis = analyze(mods)
        inputs = {'clones': str(args.clones) if args.clones else None,
                  'releases': str(args.releases) if args.releases else None,
                  'mods': sorted(selected), 'extra': sorted(extras)}
        data = graph_data(mods, analysis, inputs, args.full, args.no_timestamp)
        write(args.out, data, args.json_only)
    except (InputError, OSError, ValueError, LookupError, OverflowError, RecursionError) as exc:
        print('mod-conflict-graph: %s' % exc, file=sys.stderr)
        return 2
    if not args.quiet:
        print('%d mods; %d findings; wrote %s' % (len(mods), len(analysis['findings']), args.out))
    if args.fail_on != 'never':
        threshold = SEVERITIES.index(args.fail_on)
        if any(SEVERITIES.index(f['severity']) <= threshold for f in analysis['findings']):
            return 1
    return 0
