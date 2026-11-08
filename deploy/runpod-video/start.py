import os
from dotenv import dotenv_values
os.environ.update({key: value for key, value in dotenv_values('/data/config/runtime.env').items() if value is not None})
os.execv('/bin/bash', ['/bin/bash', '/app/deploy/runpod-video/entrypoint.sh'])
