#!/bin/sh
set -ex
SCRIPT_RELATIVE_DIR=$(dirname "${BASH_SOURCE[0]}")
cd $SCRIPT_RELATIVE_DIR

OUTPUT=`python3 epcor-water.py --zone ELS`
DATE=`date`
echo "# Results for $DATE"

echo "# ELS"
echo '''```'''
echo "$OUTPUT"
echo '''```'''

OUTPUT=`python3 epcor-water.py --zone Rossdale`
echo "# Rossdale"
echo '''```'''
echo "$OUTPUT"
echo '''```'''
