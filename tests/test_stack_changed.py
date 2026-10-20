"""Tests for `astro-stack --changed` and `astro-nightly --dry-run`, with a stand-in
for siril-cli that writes the files each pipeline step would produce.

    python3 -m unittest discover tests
"""
import os, shutil, stat, subprocess, tempfile, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STACK = os.path.join(REPO, "tools", "stack", "astro-stack.sh")
NIGHTLY = os.path.join(REPO, "tools", "nightly.sh")

# Reads the Siril script (-s FILE) and creates what that step leaves behind.
FAKE_SIRIL = r'''#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -s) S="$2"; shift 2;; -d) D="$2"; shift 2;; *) shift;; esac; done
[ -n "${SIRIL_FAIL_ON:-}" ] && grep -q -- "$SIRIL_FAIL_ON" "$S" && exit 1
W=$(awk '$1=="cd"{print $2; exit}' "$S")
OUT=$(grep -o -- '-out=[^ ]*' "$S" | tail -1 | cut -d= -f2)
if grep -q 'seqplatesolve pp_light' "$S"; then
  n=$(ls "$D" | wc -l); mkdir -p "$W"; for i in $(seq 1 "$n"); do echo "R0 x"; done > "$W/pp_light_.seq"
elif grep -q 'seqapplyreg pp_light' "$S"; then
  echo "S 'r_pp_light_' 1 5 5 0 -1 1" > "$W/r_pp_light_.seq"
elif grep -q '^stack ' "$S"; then
  echo master > "$OUT.fit"
fi
'''


def night(arc, target, name, frames):
    d = os.path.join(arc, "Targets", target, "raw", name)
    os.makedirs(d)
    for i in range(frames):
        open(os.path.join(d, "Light_%04d.fit" % i), "w").close()


def read(path):
    with open(path) as fh:
        return fh.read()


class StackChangedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arc = os.path.join(self.tmp, "Astro")
        os.makedirs(os.path.join(self.arc, "_tools"))
        open(os.path.join(self.arc, "MANIFEST.sha256"), "w").close()
        night(self.arc, "M31_Andromeda-Galaxy", "2026-10-11_S50_mosaic_IRCUT", 60)   # stacked earlier
        night(self.arc, "M31_Andromeda-Galaxy", "2026-10-19_S50_mosaic_IRCUT", 70)   # new tonight
        night(self.arc, "M31_Andromeda-Galaxy", "2026-10-19_S50_LP", 55)             # new, other filter
        night(self.arc, "M31_Andromeda-Galaxy", "2026-10-15_S30_IRCUT", 80)          # other scope, not queued
        night(self.arc, "NGC7331_Deer-Lick-Galaxy", "2026-10-19_S50_IRCUT", 12)      # too short
        os.makedirs(os.path.join(self.arc, "Targets", "Moon", "raw", "2026-10-19_S50"))
        self.queue = os.path.join(self.arc, "_tools", "stack-queue.tsv")
        with open(self.queue, "w") as fh:
            fh.write("M31_Andromeda-Galaxy\t2026-10-19_S50_mosaic_IRCUT\n"
                     "M31_Andromeda-Galaxy\t2026-10-19_S50_LP\n"
                     "NGC7331\t2026-10-19_S50_IRCUT\n"            # folder renamed since: matched by prefix
                     "Moon\t2026-10-19_S50\n"
                     "M42\t2026-10-19_S50_IRCUT\n")              # no such target
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        f = os.path.join(self.bin, "siril-cli")
        with open(f, "w") as fh:
            fh.write(FAKE_SIRIL)
        os.chmod(f, os.stat(f).st_mode | stat.S_IEXEC)
        self.env = dict(os.environ, ASTRO_ARCHIVE=self.arc, ASTRO_WORK=os.path.join(self.tmp, "work"),
                        PATH=self.bin + os.pathsep + os.environ["PATH"])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cmd(self, cmd, **env):
        p = subprocess.run(cmd, env=dict(self.env, **env), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        return p.returncode, p.stdout

    def queued(self):
        return read(self.queue).splitlines()

    def results(self):
        proc = os.path.join(self.arc, "Targets", "M31_Andromeda-Galaxy", "processing")
        return sorted(d for d in os.listdir(proc) if d != "batch-masters") if os.path.isdir(proc) else []

    def test_dry_run_plans_groups_and_leaves_queue(self):
        before = self.queued()
        rc, out = self.run_cmd(["bash", STACK, "--changed", "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn("=== M31_Andromeda-Galaxy, S50_IRCUT ===", out)
        self.assertIn("=== M31_Andromeda-Galaxy, S50_LP ===", out)
        self.assertIn("fewer than --min-frames", out)          # NGC7331 night
        self.assertIn("Moon/Sun", out)
        self.assertIn("no unique target folder matching 'M42'", out)
        self.assertNotIn("S30", out.split("=== M31_Andromeda-Galaxy, S50_IRCUT ===")[1].split("===")[0])
        self.assertEqual(self.queued(), before)
        self.assertEqual(self.results(), [])

    def test_stacks_each_group_and_empties_queue(self):
        rc, out = self.run_cmd(["bash", STACK, "--changed"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.queued(), [])
        res = self.results()
        self.assertEqual(len(res), 2, res)
        self.assertTrue(any(r.endswith("_siril-all_S50_IRCUT") for r in res), res)
        self.assertTrue(any(r.endswith("_siril-all_S50_LP") for r in res), res)
        ircut = [r for r in res if r.endswith("S50_IRCUT")][0]
        readme = read(os.path.join(self.arc, "Targets/M31_Andromeda-Galaxy/processing", ircut, "README.txt"))
        self.assertIn("2026-10-11_S50_mosaic_IRCUT", readme)   # the earlier night is in the full stack
        self.assertNotIn("S30", readme)

    def test_failed_group_stays_queued(self):
        rc, out = self.run_cmd(["bash", STACK, "--changed"], SIRIL_FAIL_ON="stack r_pp_light")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAILED", out)
        # the dropped entries are gone, both M31 groups remain for the next run
        self.assertEqual(sorted(self.queued()), ["M31_Andromeda-Galaxy\t2026-10-19_S50_LP",
                                                 "M31_Andromeda-Galaxy\t2026-10-19_S50_mosaic_IRCUT"])

    def test_nothing_queued(self):
        os.remove(self.queue)
        rc, out = self.run_cmd(["bash", STACK, "--changed"])
        self.assertEqual(rc, 0, out)
        self.assertIn("Nothing queued", out)

    def test_changed_rejects_target(self):
        rc, out = self.run_cmd(["bash", STACK, "--changed", "M31"])
        self.assertEqual(rc, 1, out)

    def test_nightly_dry_run(self):
        src = os.path.join(self.tmp, "MyWorks")
        os.makedirs(src)
        rc, out = self.run_cmd(["bash", NIGHTLY, src, "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn("astro-ingest --dry-run", out)
        self.assertIn("Nothing new to ingest", out)
        self.assertIn("=== M31_Andromeda-Galaxy, S50_IRCUT ===", out)
        self.assertEqual(len(self.queued()), 5)


if __name__ == "__main__":
    unittest.main()
