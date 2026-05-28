#!/usr/bin/env bash

full_name=$1

echo $full_name

repo=$(echo $full_name | awk -F '/' '{print $2}')

# Sanitize the repo name to be a valid Python package name
sanitized_repo=$(echo "$repo" | sed 's/[^a-zA-Z0-9_-]/_/g')

original_repo="APSBITS"

echo $sanitized_repo
echo $original_repo

sed -i "s/$original_repo/$sanitized_repo/g" README.md
# Delete the "Use this template" section from README.md
sed -i '/## Create repository from this template./,/##/d' README.md


# Update package name in pyproject.toml
sed -i "s/bits_instrument/${sanitized_repo}/g" pyproject.toml

rm -rf .github/resources
rm -rf .github/workflows/init_repo.sh
rm -rf .github/workflows/init_repo.yml
