#!/usr/bin/env bash

# Initialize conda for this shell, then activate the env
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate 3idc-bits

# Launch Ipython, run the import, and stay interactive
ipython -i -c "from id3c.startup import *"
