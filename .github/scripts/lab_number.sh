#!/bin/bash
set -e

LABRC_FILE=".labrc"

if [ ! -f "$LABRC_FILE" ]; then
  echo "❌ Error: $LABRC_FILE file not found!" >&2
  echo "Create $LABRC_FILE file with: echo 'LAB=0' > $LABRC_FILE" >&2
  exit 1
fi

LAB=$(grep -E '^LAB=' "$LABRC_FILE" | cut -d'=' -f2 | tr -d ' ')

if [ -z "$LAB" ]; then
  echo "❌ Error: LAB variable not found in $LABRC_FILE" >&2
  echo "Add to $LABRC_FILE: LAB=0" >&2
  exit 1
fi

if ! [[ "$LAB" =~ ^[0-9]+$ ]]; then
  echo "❌ Error: LAB must be a non-negative integer" >&2
  echo "Current value: LAB=$LAB" >&2
  echo "Fix $LABRC_FILE: echo 'LAB=0' > $LABRC_FILE" >&2
  exit 1
fi

echo "✅ Running checks for lab $LAB" >&2
echo "$LAB"
