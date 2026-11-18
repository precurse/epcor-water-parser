#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

status=0
echo "# Results for $(date)"

for zone in ELS Rossdale; do
    echo "# $zone"
    echo '```'
    # stdout = results only; logs/annotations go to stderr (the Actions log)
    python3 epcor-water.py --zone "$zone" || status=$?
    echo '```'
done

# Run both zones even if one fails, but still fail the job
exit "$status"
