import sys
import subprocess

script = __file__.replace("run_ultra.py", "limit_up_scanner.py")
args = [sys.executable, script, "--ultra"] + sys.argv[1:]
sys.exit(subprocess.call(args))
