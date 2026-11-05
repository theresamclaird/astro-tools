"""Tests for tools/merge_targets.py on a synthetic archive.

    python3 -m unittest discover tests
"""
import hashlib, os, shutil, subprocess, sys, tempfile, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERGE = os.path.join(REPO, "tools", "merge_targets.py")
SRC = "IC5070_Pelican-Nebula"
DST = "NGC7000_North-America-Nebula"


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arc = os.path.join(self.tmp, "Astro")
        self.manifest = []
        self.add(SRC, "2026-09-18_S50_LP", "Light_IC 5070_10.0s_LP_20260918-220000.fit")
        self.add(SRC, "2026-09-18_S50_LP", "previews/Light_IC 5070_10.0s_LP_20260918-220000.jpg")
        self.add(SRC, "2026-10-31_S50_mosaic_LP", "Light_mosaic_IC 5070_10.0s_LP_20261031-230000.fit")
        self.add(DST, "2026-10-31_S50_mosaic_LP", "Light_mosaic_NGC 7000_10.0s_LP_20261031-200000.fit")
        self.add(DST, "2026-09-27_S50_IRCUT", "Light_NGC 7000_10.0s_IRCUT_20260927-210000.fit")
        self.add_file("Targets/%s/seestar-stacks/Stacked_10_IC 5070_10.0s_LP_20260918-230000.fit" % SRC)
        os.makedirs(os.path.join(self.arc, "Targets", SRC, "final", "2026-10-01"))
        with open(os.path.join(self.arc, "Targets", DST, "notes.md"), "w") as fh:
            fh.write("# NGC 7000\n\n## Raw sessions\n\nold\n\n## Notes\n\n- keep me\n")
        os.makedirs(os.path.join(self.arc, "_tools"))
        with open(os.path.join(self.arc, "_tools", "stack-queue.tsv"), "w") as fh:
            fh.write("%s\t2026-10-31_S50_mosaic_LP\n%s\t2026-10-31_S50_mosaic_LP\n" % (SRC, DST))
        self.write_manifest()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def add(self, target, night, name):
        self.add_file("Targets/%s/raw/%s/%s" % (target, night, name))

    def add_file(self, rel, data=None):
        p = os.path.join(self.arc, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = data if data is not None else os.urandom(500)
        with open(p, "wb") as fh:
            fh.write(data)
        self.manifest.append("%s  %s" % (hashlib.sha256(data).hexdigest(), rel))

    def write_manifest(self):
        with open(os.path.join(self.arc, "MANIFEST.sha256"), "w") as fh:
            fh.write("".join(l + "\n" for l in self.manifest))

    def run_merge(self, *args):
        p = subprocess.run([sys.executable, MERGE, "IC5070", "NGC7000", "--archive", self.arc] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        return p.returncode, p.stdout

    def path(self, rel):
        return os.path.join(self.arc, *rel.split("/"))

    def manifest_ok(self):
        """Every manifest line points at an existing file with that checksum."""
        for ln in open(self.path("MANIFEST.sha256")).read().splitlines():
            digest, rel = ln[:64], ln[66:]
            with open(self.path(rel), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest, rel)

    def test_dry_run_changes_nothing(self):
        before = open(self.path("MANIFEST.sha256")).read()
        rc, out = self.run_merge("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("MERGE into existing night", out)
        self.assertTrue(os.path.exists(self.path("Targets/%s/raw/2026-09-18_S50_LP" % SRC)))
        self.assertEqual(open(self.path("MANIFEST.sha256")).read(), before)
        self.assertFalse(os.path.exists(self.path("_tools/targets.tsv")))

    def test_merge(self):
        rc, out = self.run_merge("--yes")
        self.assertEqual(rc, 0, out)
        night = "Targets/%s/raw/2026-10-31_S50_mosaic_LP/" % DST
        self.assertTrue(os.path.exists(self.path(night + "Light_mosaic_IC 5070_10.0s_LP_20261031-230000.fit")))
        self.assertTrue(os.path.exists(self.path(night + "Light_mosaic_NGC 7000_10.0s_LP_20261031-200000.fit")))
        self.assertTrue(os.path.exists(self.path(
            "Targets/%s/raw/2026-09-18_S50_LP/previews/Light_IC 5070_10.0s_LP_20260918-220000.jpg" % DST)))
        self.assertTrue(os.path.exists(self.path(
            "Targets/%s/seestar-stacks/Stacked_10_IC 5070_10.0s_LP_20260918-230000.fit" % DST)))
        self.assertFalse(os.path.exists(self.path("Targets/%s/raw" % SRC)))
        self.assertTrue(os.path.isdir(self.path("Targets/%s/final/2026-10-01" % SRC)))   # left alone
        self.manifest_ok()
        self.assertEqual(len(open(self.path("MANIFEST.sha256")).read().splitlines()), len(self.manifest))
        self.assertTrue([f for f in os.listdir(self.path("_tools")) if f.startswith("MANIFEST.sha256.before-merge")])
        self.assertEqual(open(self.path("_tools/targets.tsv")).read(), "IC 5070\t%s\n" % DST)
        self.assertEqual(open(self.path("_tools/stack-queue.tsv")).read(), "%s\t2026-10-31_S50_mosaic_LP\n" % DST)
        notes = open(self.path("Targets/%s/notes.md" % DST)).read()
        self.assertIn("2026-09-18_S50_LP", notes)
        self.assertIn("- keep me", notes)
        self.assertIn("| `2026-10-31_S50_mosaic_LP` | 2 |", notes)

    def test_rerun_is_harmless(self):
        self.run_merge("--yes")
        rc, out = self.run_merge("--yes")
        self.assertEqual(rc, 0, out)
        self.manifest_ok()
        self.assertEqual(open(self.path("_tools/targets.tsv")).read(), "IC 5070\t%s\n" % DST)

    def test_conflict_moves_nothing(self):
        clash = "Targets/%s/raw/2026-10-31_S50_mosaic_LP/Light_mosaic_IC 5070_10.0s_LP_20261031-230000.fit" % DST
        self.add_file(clash, b"different")
        self.write_manifest()
        rc, out = self.run_merge("--yes")
        self.assertEqual(rc, 1, out)
        self.assertIn("CONFLICTS", out)
        self.assertTrue(os.path.exists(self.path("Targets/%s/raw/2026-09-18_S50_LP" % SRC)))

    def test_identical_duplicate_is_dropped(self):
        rel = "Targets/%s/raw/2026-10-31_S50_mosaic_LP/Light_mosaic_IC 5070_10.0s_LP_20261031-230000.fit" % SRC
        data = open(self.path(rel), "rb").read()
        self.add_file(rel.replace(SRC, DST, 1), data)
        self.write_manifest()
        rc, out = self.run_merge("--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.path(rel)))
        self.manifest_ok()
        lines = open(self.path("MANIFEST.sha256")).read().splitlines()
        self.assertEqual(len(lines), len(set(l[66:] for l in lines)))


if __name__ == "__main__":
    unittest.main()
