#!/usr/bin/env bash
# Optional network helper; never invoked by the graph tool.
set -euo pipefail
work=/tmp/mcg-work
owner=
repos=
while (($#)); do
    case "$1" in
        --work|--owner|--repos)
            (($# >= 2)) || { echo "Missing value for $1" >&2; exit 2; }
            case "$1" in
                --work) work=$2 ;;
                --owner) owner=$2 ;;
                --repos) repos=$2 ;;
            esac
            shift 2 ;;
        *) echo "Usage: $0 --work DIR --owner OWNER --repos 'a b c'" >&2; exit 2 ;;
    esac
done
[[ $owner =~ ^[A-Za-z0-9_.-]+$ && -n $repos ]] || { echo 'Owner and repos required' >&2; exit 2; }
mkdir -p "$work/clones" "$work/releases"
for repo in $repos; do
    [[ $repo =~ ^[A-Za-z0-9_.-]+$ && $repo != . && $repo != .. ]] || exit 2
    if [[ ! -d "$work/clones/$repo" ]]; then
        timeout 180 nice -n 19 git clone --depth 1 --no-tags "https://github.com/$owner/$repo.git" "$work/clones/$repo"
    fi
    mkdir -p "$work/releases/$repo"
    if ! timeout 180 nice -n 19 gh release download --repo "$owner/$repo" --pattern '*.zip' --dir "$work/releases/$repo" --skip-existing; then
        tag=$(timeout 60 nice -n 19 gh release list --repo "$owner/$repo" --json tagName,isPrerelease --limit 100 \
            --jq '[.[] | select(.isPrerelease)][0].tagName // empty')
        if [[ -n $tag ]]; then
            timeout 180 nice -n 19 gh release download "$tag" --repo "$owner/$repo" --pattern '*.zip' --dir "$work/releases/$repo" --skip-existing
        else
            echo "No release zip found for $repo" >&2
        fi
    fi
done
printf 'python3 -m modconflict --clones %q --releases %q\n' "$work/clones" "$work/releases"
