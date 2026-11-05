"""Tests for tools/stack/sxt.py (astro-sxt) with a stand-in rc-astro.

    python3 -m unittest discover tests
"""
import os, shutil, subprocess, sys, tempfile, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SXT = os.path.join(REPO, "tools", "stack", "sxt.py")

# Mimics rc-astro: `rc-astro sxt IMAGE --output DIR/ --stars ...` writes
# DIR/<stem>-sxt.tif and, with --stars, DIR/<stem>-sxt-stars.tif.
FAKE = r"""#!/bin/sh
echo "$@" >> "$(dirname "$0")/calls"
[ "$1" = sxt ] || exit 2
img="$2"; shift 2; out=""; stars=0
while [ $# -gt 0 ]; do
  case "$1" in --output) out="$2"; shift ;; --stars) stars=1 ;; esac; shift
done
[ -n "$FAIL" ] && { echo "license not active"; exit 3; }
stem=$(basename "${img%.*}")
echo starless > "$out/$stem-sxt.tif"
[ $stars = 1 ] && echo stars > "$out/$stem-sxt-stars.tif"
exit 0
"""


class SxtTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rc = os.path.join(self.tmp, "rc-astro")
        with open(self.rc, "w") as f:
            f.write(FAKE)
        os.chmod(self.rc, 0o755)
        self.img = os.path.join(self.tmp, "proc", "ngc7000 stretched.tif")
        os.makedirs(os.path.dirname(self.img))
        with open(self.img, "w") as f:
            f.write("image")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_sxt(self, *args, **env):
        e = dict(os.environ, RC_ASTRO=self.rc)
        e.update(env)
        return subprocess.run([sys.executable, SXT] + list(args), env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def calls(self):
        with open(os.path.join(self.tmp, "calls")) as f:
            return f.read().splitlines()

    def test_splits_beside_image(self):
        r = self.run_sxt(self.img)
        self.assertEqual(r.returncode, 0, r.stdout)
        stem = os.path.splitext(self.img)[0]
        with open(stem + "_starless.tif") as f:
            self.assertEqual(f.read().strip(), "starless")
        with open(stem + "_stars.tif") as f:
            self.assertEqual(f.read().strip(), "stars")
        left = sorted(os.listdir(os.path.dirname(self.img)))
        self.assertEqual(len(left), 3, left)  # no temp folder left behind
        call = self.calls()[0]
        for opt in ("--stars", "--unscreen", "--depth 16U", "--device auto"):
            self.assertIn(opt, call)

    def test_skips_done_and_overwrite(self):
        self.run_sxt(self.img)
        r = self.run_sxt(self.img)
        self.assertIn("already split", r.stdout)
        self.assertEqual(len(self.calls()), 1)
        r = self.run_sxt(self.img, "--overwrite", "--no-unscreen")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("--unscreen", self.calls()[1])

    def test_wildcard_ignores_outputs(self):
        self.run_sxt(self.img)
        r = self.run_sxt(os.path.join(os.path.dirname(self.img), "*.tif"), "--dry-run")
        self.assertEqual(r.stdout.count("would split"), 1, r.stdout)

    def test_failure_reported(self):
        r = self.run_sxt(self.img, FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("license not active", r.stdout)
        self.assertEqual(len(os.listdir(os.path.dirname(self.img))), 1)

    def test_missing_rc_astro(self):
        r = self.run_sxt(self.img, RC_ASTRO=os.path.join(self.tmp, "nope"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rc-astro not found", r.stdout)


if __name__ == "__main__":
    unittest.main()
